"""RED scaffold — conductor token/burn graph: phase_progress.token_turns must
populate for folder-mode / MCP-handle tasks (#134, #137).

phase_progress(task_id) builds the conductor tile's per-turn burn series
(`token_turns`) and the honest `turns` total. Three confirmed defects leave the
tile blank or lying for real tasks:

  (#134) conductor_service.py:1753 `if source_path:` gates the live token read
    on a non-empty source_path. A folder-mode project that registered its
    Claude transcripts via `claude_project_dir` (override_dir) but has an
    empty/cwd-mismatched source_path reads NO tokens — blank tile forever.
    The fix: the live readers gain an `override_dir` param (mirroring
    import_unseen's v6.2.16 pattern — glob <override_dir>/*.jsonl plus nested
    <override_dir>/<session_id>/*.jsonl), phase_progress resolves it via
    claude_memory.configured_project_dir(project_id), and fires the read when
    EITHER source_path OR override_dir is set.

  (#137 primary) Only sessions_for_task feeds the read, so a task whose ONLY
    linked sessions are unresolvable 32-hex MCP handles (no transcript by that
    id) renders blank. NOTE: the project-wide wall-clock fallback that #137
    originally added here was REVERSED by task 5ecbbfb8 (owner decision: the
    per-task tile shows task-EXCLUSIVE activity ONLY). An MCP-handle-only task
    now stays honestly empty instead of borrowing the project window burn —
    see test_phase_progress_no_wallclock_fallback_for_mcp_handle_sessions below
    and tests/unit/test_conductor_token_source.py.

  (#137 secondary) The series is built from the 40-CAPPED
    live_token_turns_for_session and `total_turns` is len() AFTER per-session
    capping, so a 2,388-turn task reports `turns`==40. The fix: build the full
    UNCAPPED series from live_token_events_for_session, set total_turns BEFORE
    the [-40:] tail slice — `turns` stays honest (>40) while `token_turns` stays
    tail-capped at 40.

A single shared events->turns helper applies the 1s/600s clamp so the clamp
isn't duplicated between the live path and the wall-clock fallback.

These pin the REAL conductor seam end-to-end: phase_progress(task_id) reading
through the actual resolvers (monkeypatched at their module home, not bypassed)
with transcripts on disk — not a reader method in isolation. ALL FAIL today.
"""

from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import claude_transcripts as ct  # noqa: E402


_PID = "phase_progress_pid"


def _write_transcript(path: Path, turns: list[tuple[float, int]]) -> None:
    """Write a Claude transcript JSONL with one assistant turn per (epoch, out)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    lines = []
    for ep, tok in turns:
        ts = datetime.fromtimestamp(ep, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        lines.append(json.dumps({
            "type": "assistant",
            "timestamp": ts,
            "message": {"usage": {"output_tokens": tok}},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _conductor(tmp_path):
    """ConductorService whose scores.db lives at <tmp>/projects/<pid>/scores.db
    so phase_progress derives project_id == _PID for the resolvers."""
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    proj_dir = tmp_path / "projects" / _PID
    proj_dir.mkdir(parents=True, exist_ok=True)
    scores_db = str(proj_dir / "scores.db")
    task_svc = TaskService(str(proj_dir / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    # session_outcomes is normally created by brain_engine; materialize it here
    # (conductor-only flow) so sessions_for_task's LEFT JOIN resolves instead of
    # OperationalError-ing to [] — mirrors test_conductor_live_tokens.py setup.
    import sqlite3
    conn = sqlite3.connect(scores_db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_outcomes ("
            "session_id TEXT PRIMARY KEY, duration_s REAL, tokens_used INTEGER, "
            "files_read INTEGER, files_modified INTEGER, skills_invoked INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()
    return task_svc, cond


# ----------------------------------------------------------------------
# Unit — the three live readers gained an override_dir parameter that
# reads <override_dir>/*.jsonl + nested <override_dir>/<session_id>/.
# ----------------------------------------------------------------------

@pytest.mark.parametrize("fn_name", [
    "live_tokens_for_session",
    "live_token_events_for_session",
    "project_token_events_in_window",
])
def test_live_readers_accept_override_dir(fn_name):
    fn = getattr(ct, fn_name)
    params = inspect.signature(fn).parameters
    assert "override_dir" in params, (
        f"{fn_name} must accept an override_dir param (mirroring import_unseen)"
    )


def test_override_dir_reads_flat_and_nested_jsonl(tmp_path):
    """live_token_events_for_session(override_dir=d) reads <d>/<sid>.jsonl AND
    nested workflow-subagent transcripts under <d>/<sid>/ — without touching
    ~/.claude/projects (project_path empty)."""
    sid = "11111111-1111-1111-1111-111111111111"
    d = tmp_path / "claudedir"
    _write_transcript(d / f"{sid}.jsonl", [(1000.0, 100), (1002.0, 200)])
    _write_transcript(d / sid / "subagent.jsonl", [(1004.0, 50)])

    events = ct.live_token_events_for_session(sid, "", override_dir=str(d))
    toks = sorted(tok for _, tok in events)
    assert toks == [50, 100, 200], events


def test_project_window_override_dir_buckets_by_wallclock(tmp_path):
    """project_token_events_in_window(override_dir=d) buckets ALL <d> transcript
    turns in [since, until] — the MCP-handle fallback source."""
    d = tmp_path / "claudedir"
    _write_transcript(d / "aaaa.jsonl", [(1000.0, 10), (1500.0, 20), (9999.0, 999)])

    events = ct.project_token_events_in_window("", 900.0, 2000.0, override_dir=str(d))
    toks = sorted(tok for _, tok in events)
    assert toks == [10, 20], events  # the 9999 turn is out of window


# ----------------------------------------------------------------------
# Oracle (a) — override_dir population with EMPTY source_path (#134).
# The conductor tile must show real token_turns for a folder-mode project
# that registered claude_project_dir but has no/empty source_path.
# ----------------------------------------------------------------------

def _patch_resolvers(monkeypatch, source_path: str, override_dir: str):
    """Bind the two phase_progress resolvers at their module homes (the REAL
    seam): claude_transcripts._project_source_path and
    claude_memory.configured_project_dir."""
    monkeypatch.setattr(ct, "_project_source_path", lambda pid: source_path)
    from prism_service.services import claude_memory as cm
    monkeypatch.setattr(cm, "configured_project_dir", lambda pid: override_dir)


def test_phase_progress_populates_from_override_dir_empty_source(tmp_path, monkeypatch):
    task_svc, cond = _conductor(tmp_path)
    sid = "22222222-2222-2222-2222-222222222222"
    d = tmp_path / "claudedir"
    now = time.time()
    _write_transcript(d / f"{sid}.jsonl", [(now - 30, 100), (now - 20, 150), (now - 10, 200)])

    t = task_svc.create(title="folder-mode tile")
    cond.advance_task(t.id)  # onto the workflow so it's a managed task
    task_svc.link_session(t.id, sid)

    # EMPTY source_path, but claude_project_dir (override_dir) is registered.
    _patch_resolvers(monkeypatch, source_path="", override_dir=str(d))

    pp = cond.phase_progress(t.id)
    assert pp["token_turns"], (
        "token_turns must populate from override_dir even when source_path is "
        "empty (#134) — got empty series"
    )
    assert all({"out", "dt_s", "tok_s"} <= set(turn) for turn in pp["token_turns"])


# ----------------------------------------------------------------------
# Oracle (b) — REVERSED CONTRACT (task 5ecbbfb8, owner decision): the per-task
# tile shows task-EXCLUSIVE activity ONLY. The old #137 project-wide wall-clock
# fallback (an in_progress MCP-handle task borrowing the project's window burn)
# is DROPPED — superseded by tests/unit/test_conductor_token_source.py. An
# in_progress task whose only link is an unresolvable MCP handle now stays
# honestly empty rather than painting another task's burn.
# ----------------------------------------------------------------------

def test_phase_progress_no_wallclock_fallback_for_mcp_handle_sessions(tmp_path, monkeypatch):
    task_svc, cond = _conductor(tmp_path)
    # The only linked "session" is a 32-hex MCP request handle — there is NO
    # transcript named <handle>.jsonl. The work is on disk under a DIFFERENT id.
    mcp_handle = "deadbeefdeadbeefdeadbeefdeadbeef"
    real_sid = "33333333-3333-3333-3333-333333333333"
    d = tmp_path / "claudedir"
    now = time.time()
    _write_transcript(d / f"{real_sid}.jsonl", [(now - 40, 80), (now - 25, 120), (now - 5, 160)])

    t = task_svc.create(title="mcp-handle tile")
    cond.advance_task(t.id)
    task_svc.link_session(t.id, mcp_handle)
    # Even an in_progress task no longer borrows the project-wide wall-clock burn
    # (task 5ecbbfb8 reverses the #134/#137 fallback for the per-task surface).
    task_svc.update(t.id, status="in_progress")

    _patch_resolvers(monkeypatch, source_path=str(tmp_path / "src"), override_dir=str(d))

    pp = cond.phase_progress(t.id)
    assert pp["token_turns"] == [], (
        "the per-task tile must NOT borrow project-wide wall-clock burn for an "
        "MCP-handle-only task — task-exclusive activity ONLY (task 5ecbbfb8)"
    )
    assert pp["tokens_source"] == "linked", pp["tokens_source"]
    assert pp["turns"] == 0, pp["turns"]
    assert pp["tokens_since_step"] == 0, pp["tokens_since_step"]


# ----------------------------------------------------------------------
# Oracle (c) — honest turns>40 with a tail-capped token_turns (#137 secondary).
# ----------------------------------------------------------------------

def test_phase_progress_turns_total_is_honest_above_cap(tmp_path, monkeypatch):
    task_svc, cond = _conductor(tmp_path)
    sid = "44444444-4444-4444-4444-444444444444"
    d = tmp_path / "claudedir"
    now = time.time()
    # 50 turns, each 2s apart — well above the 40-turn tail cap.
    turns = [(now - (50 - i) * 2, 100 + i) for i in range(50)]
    _write_transcript(d / f"{sid}.jsonl", turns)

    t = task_svc.create(title="long task")
    cond.advance_task(t.id)
    task_svc.link_session(t.id, sid)
    _patch_resolvers(monkeypatch, source_path="", override_dir=str(d))

    pp = cond.phase_progress(t.id)
    assert pp["turns"] == 50, (
        f"`turns` must report the HONEST total (50), not the capped count — "
        f"got {pp['turns']} (#137 secondary)"
    )
    assert len(pp["token_turns"]) == 40, (
        f"token_turns must stay tail-capped at 40, got {len(pp['token_turns'])}"
    )
