"""Regression — conductor tiles must show LIVE token effort, not 0 tok
(v6.3.1).

Two bugs left the SDLC progress bar stuck at "0 tok" for any in-progress task:

  Bug 1 (sourcing) — phase_progress only read the post-hoc session_outcomes
    table, which is empty until a session is imported. An IN-PROGRESS task
    therefore always read 0. Fix: also sum output_tokens off the live
    transcript JSONL on disk (parent + nested workflow-subagent transcripts)
    and take the max.
  Bug 2 (linking) — conductor_advance/gate fell back to the MCP request_id
    when no session_id was passed; that phantom maps to no transcript and no
    token row, so workflow-driven tasks linked dead sessions. Fix:
    current_session_id() resolves the real active transcript session.

These pin the disk-read seam end-to-end with a fabricated ~/.claude tree.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _write_jsonl(path: Path, session_id: str, out_tokens: list[int]) -> None:
    """Emit a minimal transcript: one assistant event per output-token count.

    Carries a real "timestamp" (task 170991cc, regression from 569e7d0 /
    #316 "SCOPE THE STEP READOUT TO THE STEP"): claude_transcripts._token_events
    parses evt["timestamp"] via _parse_iso_epoch and `continue`s past any line
    where that comes back None, so a timestamp-less synthetic line is silently
    invisible to the per-turn windowed reader (tokens_since_step) even though
    the older lifetime-sum reader (live_tokens_for_session) still counts it."""
    from datetime import datetime, timezone

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for n in out_tokens:
        lines.append(json.dumps({
            "sessionId": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {"role": "assistant", "usage": {"output_tokens": n}},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fake_tree(tmp_path: Path, write: bool = True):
    """Build claude_home/projects/<slug>/ paths for a parent transcript + one
    nested workflow-subagent transcript. Returns (claude_home, source_path,
    sess). write=False skips writing the JSONL content immediately - a
    caller that needs the transcript to land AFTER some other event (e.g. a
    step-entry timestamp the windowed reader anchors to) calls
    _write_fake_transcripts() itself once that event has happened."""
    source_path = str(tmp_path / "src" / "proj")
    claude_home = tmp_path / "claude"
    sess = "11111111-2222-3333-4444-555555555555"
    if write:
        _write_fake_transcripts(claude_home, source_path, sess)
    return claude_home, source_path, sess


def _write_fake_transcripts(claude_home: Path, source_path: str, sess: str) -> None:
    """Write the parent (300 tok) + nested subagent (125 tok) transcripts for
    _fake_tree's session. Split out of _fake_tree so a caller can control
    WHEN they hit disk relative to some other timestamped event."""
    from prism_service.services.claude_transcripts import path_to_slug

    proj = claude_home / "projects" / path_to_slug(source_path)
    _write_jsonl(proj / f"{sess}.jsonl", sess, [100, 200])  # parent = 300
    # subagent transcript nested under the session dir carries the parent id
    _write_jsonl(
        proj / sess / "subagents" / "wf_x" / "agent-abc.jsonl", sess, [50, 75],
    )  # subagent = 125


# ----------------------------------------------------------------------
# Bug 1 — live transcript token read (parent + subagents), with cache
# ----------------------------------------------------------------------

def test_live_tokens_sums_parent_and_subagent_transcripts(tmp_path):
    from prism_service.services import claude_transcripts as ct

    claude_home, source_path, sess = _fake_tree(tmp_path)
    total = ct.live_tokens_for_session(sess, source_path, claude_home=claude_home)
    assert total == 425, f"expected 300 parent + 125 subagent = 425, got {total}"


def test_live_tokens_zero_for_unknown_session(tmp_path):
    from prism_service.services import claude_transcripts as ct

    claude_home, source_path, _ = _fake_tree(tmp_path)
    # A phantom id (the old request_id symptom) resolves to no transcript.
    assert ct.live_tokens_for_session(
        "deadbeef" * 4, source_path, claude_home=claude_home,
    ) == 0


# ----------------------------------------------------------------------
# Bug 2 — current_session_id resolves the real active transcript session
# ----------------------------------------------------------------------

def test_current_session_id_returns_real_session(tmp_path):
    from prism_service.services import claude_transcripts as ct

    claude_home, source_path, sess = _fake_tree(tmp_path)
    assert ct.current_session_id(source_path, claude_home=claude_home) == sess


# ----------------------------------------------------------------------
# End-to-end — phase_progress reports live tokens with NO outcomes row
# (the exact in-progress state that previously read 0 tok)
# ----------------------------------------------------------------------

def test_phase_progress_live_tokens_without_outcomes_row(tmp_path, monkeypatch):
    """Task 170991cc. Regression commit 569e7d0 (#316, "The conductor tells
    the truth about what it is doing") scoped the readout to a per-turn
    windowed timeline (conductor_service.py:4551-4564) that only counts
    transcript lines carrying a parseable "timestamp" AND at/after the
    current step's entry instant (claude_transcripts.tokens_in_window).
    This fixture's _write_jsonl predated that change and never grew a
    timestamp, so the windowed reader skipped every line
    (claude_transcripts._token_events, "if ep is None: continue") and
    tokens_since_step silently read 0 on any in-progress task. Two fixes,
    both confined to this fixture (never conductor_service.py/
    claude_transcripts.py, both product-correct): (1) _write_jsonl now
    stamps a real timestamp; (2) the transcript is written AFTER
    advance_task (with a margin past phase_progress's own internal
    now()-sampling delay), because a step-entry-anchored window can never
    include a transcript that predates step entry, no matter how it is
    timestamped - this mirrors reality, where a session's turns happen
    while the task is in-progress, not before."""
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services import claude_transcripts as ct

    import sqlite3

    claude_home, source_path, sess = _fake_tree(tmp_path, write=False)
    scores_db = str(tmp_path / "scores.db")
    # session_outcomes is normally created by brain_engine; materialize it
    # EMPTY here to mirror an in-progress session the importer hasn't written
    # a row for yet (the exact 0-tok scenario this fix targets).
    conn = sqlite3.connect(scores_db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_outcomes ("
        "session_id TEXT PRIMARY KEY, duration_s INTEGER, tokens_used INTEGER, "
        "files_read INTEGER, files_modified INTEGER, skills_invoked INTEGER, "
        "timestamp TEXT)"
    )
    conn.commit()
    conn.close()
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    # Source path normally comes off understand_state; pin it for the test.
    monkeypatch.setattr(cond, "_project_source_path", lambda: source_path)
    monkeypatch.setattr(
        ct, "resolve_claude_home", lambda: claude_home,
    )

    t = task_svc.create(title="in-progress, no outcome row")
    cond.advance_task(t.id)
    # The transcript must land AFTER the step-entry timestamp advance_task
    # just stamped: tokens_in_window only counts events at/after it, and
    # phase_progress itself burns real wall-clock between its two internal
    # now() samples (measured ~30ms on this host), so give a generous margin.
    import time
    time.sleep(0.5)
    _write_fake_transcripts(claude_home, source_path, sess)
    # Link the REAL session — but DO NOT write a session_outcomes row, mirroring
    # an in-progress task the importer hasn't seen yet.
    task_svc.link_session(t.id, sess)

    pp = cond.phase_progress(t.id)
    assert pp["tokens_since_step"] == 425, (
        "in-progress task must report live transcript tokens (425) even with "
        f"no session_outcomes row; got {pp['tokens_since_step']}"
    )
