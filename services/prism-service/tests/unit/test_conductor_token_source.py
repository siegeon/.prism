"""RED scaffold — conductor per-task token HONESTY (follow-up to #134/#137).

#137 made phase_progress.token_turns populate via override_dir + a project-wide
wall-clock fallback. That shipped two NEW lies the conductor tile now tells:

  PROBLEM 1 — authoritative linkage still misses. _resolve_link_session_id
    (tools.py:1559) only resolves the real transcript session when
    claude_transcripts._project_source_path(project_id) is TRUTHY (tools.py:1573).
    In folder-mode / cwd-mismatch (#134) src='' so it falls straight through to
    ctx.request_id (tools.py:1579) — the 32-hex MCP request handle that maps to
    NO transcript. current_session_id (claude_transcripts.py:910) does not yet
    accept override_dir, so even with an explicit claude_project_dir configured
    the link is stamped with the phantom handle. The downstream wall-clock
    fallback then fires for EVERY task, painting parked tiles with the worked
    tile's burn (the 2625-turns duplication).

  PROBLEM 2 — the wall-clock fallback is project-WIDE and unconditional. A
    pending / parked task with no authoritative live_events gets the SAME
    project window series as the task actually being worked (conductor_service
    .py:1840 `if not live_events and window_fn:`), so two tiles plot one series.
    And the fallback fills token_turns (the graph) but never updates `tokens`
    (conductor_service.py:1829 only), so the tile reads "2625 turns" while
    tokens_since_step stays 0 — the "graph full / number 0" contradiction.

THE FIX this file pins:
  (a) current_session_id gains override_dir; _resolve_link_session_id resolves
      override_dir via claude_memory.configured_project_dir and returns the REAL
      session id (not ctx.request_id) when source_path='' + override_dir-set.
  (b) phase_progress applies the wall-clock fallback ONLY when the driven task's
      status == 'in_progress'. A pending task with no authoritative turns →
      token_turns empty + a NEW tokens_source field == 'linked'. An in_progress
      task with no live events → wall-clock fill tagged tokens_source=='wallclock'.
  (c) when tokens_source=='wallclock', tokens_since_step is fed from the SAME
      fallback events: > 0 AND == the series event sum (no graph/number lie).

These pin the REAL seams: the link resolver through its request-context entry
and the conductor service's phase_progress reading through the actual resolvers
(monkeypatched at their module home, not bypassed) with transcripts on disk.
A UI guard asserts TokenTurns.tsx consumes the tokens_source prop. ALL FAIL today.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import time
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import claude_transcripts as ct  # noqa: E402

_PID = "token_source_pid"


def _write_transcript(path: Path, turns: list[tuple[float, int]], sid: str = "") -> None:
    """Write a Claude transcript JSONL: one assistant turn per (epoch, out).
    When `sid` is given, each line carries that sessionId so _session_id_of
    resolves it (current_session_id reads sessionId off the newest file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    lines = []
    for ep, tok in turns:
        ts = datetime.fromtimestamp(ep, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        evt = {"type": "assistant", "timestamp": ts,
               "message": {"usage": {"output_tokens": tok}}}
        if sid:
            evt["sessionId"] = sid
        lines.append(json.dumps(evt))
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


def _patch_resolvers(monkeypatch, source_path: str, override_dir: str):
    """Bind phase_progress's resolvers at their module homes (the REAL seam)."""
    monkeypatch.setattr(ct, "_project_source_path", lambda pid: source_path)
    from prism_service.services import claude_memory as cm
    monkeypatch.setattr(cm, "configured_project_dir", lambda pid: override_dir)


# ----------------------------------------------------------------------
# Oracle (a) — authoritative link-session resolution.
# current_session_id gains override_dir; _resolve_link_session_id returns the
# REAL session id (not ctx.request_id) when source_path='' + override_dir set.
# ----------------------------------------------------------------------

def test_current_session_id_accepts_override_dir_param():
    params = inspect.signature(ct.current_session_id).parameters
    assert "override_dir" in params, (
        "current_session_id must accept an override_dir param mirroring the "
        "v6.3.21 readers (live_tokens_for_session etc.)"
    )


def test_current_session_id_resolves_real_sid_from_override_dir(tmp_path):
    """With project_path empty but override_dir set, current_session_id resolves
    the active transcript's sessionId via _override_session_paths — the REAL id,
    not ''."""
    real_sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    d = tmp_path / "claudedir"
    _write_transcript(d / f"{real_sid}.jsonl", [(time.time() - 5, 120)], sid=real_sid)

    got = ct.current_session_id("", override_dir=str(d))
    assert got == real_sid, (
        f"current_session_id(override_dir) must return the real sessionId off "
        f"disk, got {got!r}"
    )


def test_resolve_link_session_id_returns_real_not_request_handle(tmp_path, monkeypatch):
    """_resolve_link_session_id must return the REAL transcript session id (not
    ctx.request_id) when source_path is '' but claude_project_dir is configured.
    This is the linkage fix that stops the phantom MCP-handle stamp."""
    from prism_service.mcp import tools as mcp_tools
    from prism_service.mcp.request_context import (
        PrismRequestContext, use_request_context,
    )

    real_sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    request_handle = "deadbeefdeadbeefdeadbeefdeadbeef"
    d = tmp_path / "claudedir"
    _write_transcript(d / f"{real_sid}.jsonl", [(time.time() - 3, 90)], sid=real_sid)

    # Folder-mode: source_path EMPTY, but claude_project_dir IS configured.
    monkeypatch.setattr(ct, "_project_source_path", lambda pid: "")
    from prism_service.services import claude_memory as cm
    monkeypatch.setattr(cm, "configured_project_dir", lambda pid: str(d))

    ctx = PrismRequestContext(project_id=_PID, request_id=request_handle)
    with use_request_context(ctx):
        got = mcp_tools._resolve_link_session_id()

    assert got == real_sid, (
        f"_resolve_link_session_id must resolve the REAL session via override_dir "
        f"when source_path is empty — got {got!r} (request_handle would be the bug)"
    )
    assert got != request_handle, "must NOT fall through to the MCP request handle"


# ----------------------------------------------------------------------
# Oracle (b) — pending-LINKED vs in_progress-WALLCLOCK gate + tokens_source flag.
# ----------------------------------------------------------------------

def test_phase_progress_has_tokens_source_field(tmp_path, monkeypatch):
    task_svc, cond = _conductor(tmp_path)
    t = task_svc.create(title="needs-source-field")
    cond.advance_task(t.id)
    _patch_resolvers(monkeypatch, source_path="", override_dir=str(tmp_path / "empty"))

    pp = cond.phase_progress(t.id)
    assert "tokens_source" in pp, "phase_progress must return a tokens_source field"
    assert pp["tokens_source"] in ("linked", "wallclock"), pp["tokens_source"]


def test_pending_task_no_authoritative_turns_stays_linked_and_empty(tmp_path, monkeypatch):
    """A PENDING task whose only linked session is an unresolvable MCP handle
    must NOT borrow another task's project-wide burn: token_turns empty,
    tokens_source=='linked'. (No painting a parked tile.)"""
    task_svc, cond = _conductor(tmp_path)
    mcp_handle = "deadbeefdeadbeefdeadbeefdeadbeef"
    other_sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    d = tmp_path / "claudedir"
    now = time.time()
    # There IS project activity on disk (a DIFFERENT session) — the old code
    # would bucket it into this parked task's window.
    _write_transcript(d / f"{other_sid}.jsonl", [(now - 30, 100), (now - 10, 200)], sid=other_sid)

    t = task_svc.create(title="parked tile")
    cond.advance_task(t.id)
    task_svc.link_session(t.id, mcp_handle)
    task_svc.update(t.id, status="pending")
    _patch_resolvers(monkeypatch, source_path="", override_dir=str(d))

    pp = cond.phase_progress(t.id)
    assert pp["tokens_source"] == "linked", (
        f"a pending task must stay tokens_source=='linked', got {pp['tokens_source']!r}"
    )
    assert pp["token_turns"] == [], (
        f"a pending task with no authoritative turns must NOT borrow project-wide "
        f"burn — got {len(pp['token_turns'])} turns"
    )


def test_in_progress_task_no_live_events_fills_via_wallclock(tmp_path, monkeypatch):
    """An IN_PROGRESS task whose only linked session is an unresolvable MCP
    handle fills via the project-wide wall-clock fallback, tagged
    tokens_source=='wallclock'."""
    task_svc, cond = _conductor(tmp_path)
    mcp_handle = "deadbeefdeadbeefdeadbeefdeadbeef"
    real_sid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    d = tmp_path / "claudedir"
    now = time.time()
    _write_transcript(d / f"{real_sid}.jsonl",
                      [(now - 40, 80), (now - 25, 120), (now - 5, 160)], sid=real_sid)

    t = task_svc.create(title="worked tile")
    cond.advance_task(t.id)
    task_svc.link_session(t.id, mcp_handle)
    task_svc.update(t.id, status="in_progress")
    _patch_resolvers(monkeypatch, source_path=str(tmp_path / "src"), override_dir=str(d))

    pp = cond.phase_progress(t.id)
    assert pp["token_turns"], (
        "an in_progress task with no live events must fill via the wall-clock "
        "fallback — got empty series"
    )
    assert pp["tokens_source"] == "wallclock", (
        f"wall-clock-derived series must be tagged tokens_source=='wallclock', "
        f"got {pp['tokens_source']!r}"
    )


# ----------------------------------------------------------------------
# Oracle (c) — wall-clock tokens_since_step == series sum (no graph/number lie).
# ----------------------------------------------------------------------

def test_wallclock_tokens_since_step_equals_series_sum(tmp_path, monkeypatch):
    """When tokens_source=='wallclock', tokens_since_step is fed from the SAME
    fallback events: > 0 AND == the sum of the series' per-turn output tokens.
    No '2625 turns / 0 tokens' contradiction."""
    task_svc, cond = _conductor(tmp_path)
    mcp_handle = "deadbeefdeadbeefdeadbeefdeadbeef"
    real_sid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    d = tmp_path / "claudedir"
    now = time.time()
    turns_in = [(now - 40, 80), (now - 25, 120), (now - 5, 160)]
    _write_transcript(d / f"{real_sid}.jsonl", turns_in, sid=real_sid)

    t = task_svc.create(title="worked tile sum")
    cond.advance_task(t.id)
    task_svc.link_session(t.id, mcp_handle)
    task_svc.update(t.id, status="in_progress")
    _patch_resolvers(monkeypatch, source_path=str(tmp_path / "src"), override_dir=str(d))

    pp = cond.phase_progress(t.id)
    assert pp["tokens_source"] == "wallclock", pp["tokens_source"]
    series_sum = sum(int(turn["out"]) for turn in pp["token_turns"])
    assert series_sum > 0, "wall-clock series must carry real per-turn tokens"
    assert pp["tokens_since_step"] == series_sum, (
        f"tokens_since_step ({pp['tokens_since_step']}) must equal the wall-clock "
        f"series sum ({series_sum}) — the graph and the number must agree"
    )


# ----------------------------------------------------------------------
# UI guard — TokenTurns.tsx must consume a tokens_source prop and dim/label
# the 'wallclock' (project activity) case. Asserts the real UI seam exists.
# ----------------------------------------------------------------------

def test_token_turns_component_consumes_tokens_source_prop():
    tsx = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "components"
           / "conductor" / "TokenTurns.tsx").read_text(encoding="utf-8")
    assert "tokens_source" in tsx or "tokensSource" in tsx, (
        "TokenTurns.tsx must consume a tokens_source / tokensSource prop"
    )
    assert re.search(r"project activity", tsx, re.IGNORECASE), (
        "TokenTurns.tsx must label the wall-clock case 'project activity "
        "(approximate)'"
    )
