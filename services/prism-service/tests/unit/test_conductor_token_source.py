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
  (b) phase_progress drops the project-wide wall-clock fallback entirely for the
      per-task tile (owner decision: task-exclusive activity ONLY). A pending OR
      in_progress task with no authoritative linked-session turns → token_turns
      empty, turns==0, tokens_since_step==0, tokens_source=='linked'. An
      in_progress task WITH real linked events still reports its own per-task
      series (tokens_source=='linked').

NOTE: the legacy wall-clock-fill tests (in_progress→'wallclock' and
  wallclock-tokens_since_step==series-sum) were the OLD contract that this task
  reverses; they are replaced by the linked/empty/0 assertions below.

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


def test_in_progress_task_no_live_events_stays_linked_and_empty(tmp_path, monkeypatch):
    """REVERSED CONTRACT (per-task tile, owner decision): an IN_PROGRESS task
    whose only linked session is an unresolvable MCP handle must NOT borrow the
    project-wide wall-clock burn. The per-task tile shows task-exclusive activity
    ONLY, so with no authoritative linked-session events the series is honestly
    empty: tokens_source=='linked', token_turns==[], turns==0,
    tokens_since_step==0. (Drops the #134/#137 wall-clock fallback.)"""
    task_svc, cond = _conductor(tmp_path)
    mcp_handle = "deadbeefdeadbeefdeadbeefdeadbeef"
    real_sid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    d = tmp_path / "claudedir"
    now = time.time()
    # There IS project activity on disk (a DIFFERENT, unlinked session) — the
    # old code bucketed this into the worked task's window via wall-clock.
    _write_transcript(d / f"{real_sid}.jsonl",
                      [(now - 40, 80), (now - 25, 120), (now - 5, 160)], sid=real_sid)

    t = task_svc.create(title="worked tile")
    cond.advance_task(t.id)
    task_svc.link_session(t.id, mcp_handle)
    task_svc.update(t.id, status="in_progress")
    _patch_resolvers(monkeypatch, source_path=str(tmp_path / "src"), override_dir=str(d))

    pp = cond.phase_progress(t.id)
    assert pp["tokens_source"] == "linked", (
        f"an in_progress task with no linked-session events must stay "
        f"tokens_source=='linked' (no project-wide wall-clock fallback), "
        f"got {pp['tokens_source']!r}"
    )
    assert pp["token_turns"] == [], (
        f"per-task tile must show task-exclusive activity only — no project-wide "
        f"burn may leak in. Got {len(pp['token_turns'])} turns"
    )
    assert pp["turns"] == 0, f"turns must be 0, got {pp['turns']}"
    assert pp["tokens_since_step"] == 0, (
        f"tokens_since_step must be 0 (no project-wide token sum leaks in), "
        f"got {pp['tokens_since_step']}"
    )


def test_in_progress_task_with_linked_events_still_reports_real_per_task(tmp_path, monkeypatch):
    """An IN_PROGRESS task WITH real authoritative linked-session events still
    reports its own per-task series: token_turns non-empty, turns>0, real
    tokens, tokens_source=='linked'. The reversal only drops the project-wide
    fallback — it must NOT suppress genuine per-task linkage."""
    task_svc, cond = _conductor(tmp_path)
    real_sid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    d = tmp_path / "claudedir"
    now = time.time()
    turns_in = [(now - 40, 80), (now - 25, 120), (now - 5, 160)]
    _write_transcript(d / f"{real_sid}.jsonl", turns_in, sid=real_sid)

    t = task_svc.create(title="real linked tile")
    cond.advance_task(t.id)
    # Link the REAL transcript session id (resolvable -> live events flow).
    task_svc.link_session(t.id, real_sid)
    task_svc.update(t.id, status="in_progress")
    _patch_resolvers(monkeypatch, source_path=str(tmp_path / "src"), override_dir=str(d))

    pp = cond.phase_progress(t.id)
    assert pp["tokens_source"] == "linked", pp["tokens_source"]
    assert pp["token_turns"], (
        "an in_progress task with real linked-session events must report its "
        "own per-task burn series — got empty"
    )
    assert pp["turns"] > 0, f"turns must reflect the real series, got {pp['turns']}"
    # SUPERSEDED 2026-08-06: this used to assert `tokens_since_step`, which
    # despite its name carried the TASK TOTAL across every linked session's
    # lifetime. That is what rendered "411M tok" beside a 49-second step, and
    # "review previous notes - 7.5M tok - 49s" (~153k tok/s, impossible).
    # `tokens_since_step` is now scoped to the current step's window, so
    # immediately after an advance_task it is legitimately 0 and can no
    # longer carry this guard.
    #
    # The INVARIANT is unchanged and still worth having: a task with real
    # linked-session events must never report zero tokens (the historic
    # "2625 turns / 0 tokens" contradiction). It now rides the field that
    # actually holds that number.
    assert pp["tokens_task_total"] > 0, (
        f"tokens_task_total must reflect real linked tokens, "
        f"got {pp['tokens_task_total']}"
    )
    # A step's spend can never exceed the task's lifetime spend.
    assert pp["tokens_since_step"] <= pp["tokens_task_total"]


# ----------------------------------------------------------------------
# UI guard — UN-RETIRED (owner 2026-07-22: "its supposed to show the wave
# form of token per second").
#
# This AC was retired because the tile did not render <TokenTurns> at all, so
# a source-scan of TokenTurns.tsx proved nothing a user could see — it was
# laundering a scan of an unrendered component. That retirement recorded a
# FACT, not a decision that the graph should not exist: the component was
# built, the server already served phase_progress.token_turns, and three files
# described it in comments as "the live TokenTurns graph beside each conductor
# tile". Nothing imported it, so a tile mid-drive looked static while a driver
# burned hundreds of thousands of tokens.
#
# The tile now renders it, so the AC is delivered again and pinned by a test
# that fails if it regresses — the manifest's own rule (add/retire an AC here,
# never re-point a proof at a comment or an unrendered feature).
# ----------------------------------------------------------------------

def test_tile_renders_the_token_burn_graph():
    """The conductor tile shows the per-turn tok/s waveform."""
    page = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
            / "ConductorPage.tsx").read_text(encoding="utf-8")
    assert "<TokenTurns" in page, (
        "the conductor tile must render the TokenTurns burn graph — without "
        "it a tile mid-drive looks static while a driver burns tokens"
    )
    assert "import TokenTurns" in page, (
        "TokenTurns must be imported, not merely mentioned in a comment — "
        "that is exactly how it sat as dead code"
    )


def test_tile_burn_graph_consumes_tokens_source():
    """The wall-clock fallback must reach the component, so an approximate
    series can be rendered dimmed/labelled instead of posing as per-task
    truth. Passing `turns` without `tokens_source` would silently launder a
    project-wide estimate into a per-task claim."""
    page = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
            / "ConductorPage.tsx").read_text(encoding="utf-8")
    assert "tokens_source={task.phase_progress?.tokens_source}" in page, (
        "the tile must pass phase_progress.tokens_source into TokenTurns so "
        "a 'wallclock' series is labelled rather than presented as linked"
    )
