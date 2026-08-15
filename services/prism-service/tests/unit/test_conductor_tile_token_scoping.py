"""RED — conductor tile burn is attributed per SESSION, not per (task,
session) LINK (task a91f1d11).

Exact regression pinned in memory (mx-56d049) and this task's oracle: session
daffe20c was linked to BOTH task 170991cc and task 2419bff2 and painted
BYTE-IDENTICAL tok_total=52,885,559 on both conductor tiles. Root cause in
ConductorService.phase_progress (conductor_service.py:4508-4527): the
per-session live-transcript read is summed for EVERY task the session is
linked to, unconditionally, with no check for whether the task was actually
DRIVEN under that session (agent_runs rows / conductor step transitions) vs.
merely linked (e.g. a locate/context sweep on an unrelated task calling
task_link_session).

THE FIX this file pins (contested-session ownership):
  - A session linked to exactly ONE task is UNCONTESTED — behaves byte-for-
    byte as today (guarded separately by test_conductor_token_source.py,
    NEVER touched by this slice).
  - A session linked to TWO OR MORE tasks is CONTESTED. A task with agent_runs
    evidence of actually driving work under that session (a real
    (task_id, session_id) row — the per-task, per-step token-attribution spine
    already written by ConductorService._record_agent_run on every
    advance_task) keeps reporting its own real burn. A task whose only claim
    to the same session is a bare task_sessions link, with NO agent_runs
    evidence, must NOT inherit that burn: tokens_task_total reads 0, not the
    other task's number.
  - Epic/child aggregation via _child_task_ids stays intact: a child that
    genuinely drove its own (uncontested) session still rolls up into its
    parent's non-zero burn (owner 2026-08-06).

ALL assertions below FAIL today because phase_progress sums every linked
session's full lifetime tokens for every task it is linked to, with no
ownership check.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PID = "tile_scoping_pid"


def _write_transcript(path: Path, turns: list[tuple[float, int]], sid: str) -> None:
    """Write a Claude transcript JSONL: one assistant turn per (epoch, out),
    all tagged with sessionId=sid so the live readers resolve it."""
    from datetime import datetime, timezone

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for ep, tok in turns:
        ts = datetime.fromtimestamp(ep, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        evt = {"type": "assistant", "timestamp": ts, "sessionId": sid,
               "message": {"usage": {"output_tokens": tok}}}
        lines.append(json.dumps(evt))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _conductor(tmp_path):
    """ConductorService + TaskService sharing one scores.db, with the
    session_outcomes and agent_runs tables materialized (mirrors the
    brain_engine schema this isolated scores.db normally inherits)."""
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    proj_dir = tmp_path / "projects" / _PID
    proj_dir.mkdir(parents=True, exist_ok=True)
    scores_db = str(proj_dir / "scores.db")
    task_svc = TaskService(str(proj_dir / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    conn = sqlite3.connect(scores_db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_outcomes ("
            "session_id TEXT PRIMARY KEY, duration_s REAL, tokens_used INTEGER, "
            "files_read INTEGER, files_modified INTEGER, skills_invoked INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_runs ("
            "run_id TEXT NOT NULL, workflow_name TEXT, task_id TEXT, "
            "session_id TEXT, agent_id TEXT NOT NULL, parent_agent_id TEXT, "
            "role TEXT, step TEXT NOT NULL, model TEXT, started_at TEXT, "
            "ended_at TEXT, duration_ms INTEGER, tokens INTEGER, "
            "tool_uses INTEGER, ok INTEGER, gate_state TEXT, "
            "verdict_summary TEXT, evidence_ref TEXT, "
            "recorded_at TEXT DEFAULT (datetime('now')), "
            "PRIMARY KEY (run_id, agent_id, step))"
        )
        conn.commit()
    finally:
        conn.close()
    return task_svc, cond, scores_db


def _patch_resolvers(monkeypatch, source_path: str, override_dir: str):
    """Bind phase_progress's resolvers at their module homes (the REAL seam,
    same pattern as test_conductor_token_source.py)."""
    from prism_service.services import claude_transcripts as ct
    from prism_service.services import claude_memory as cm

    monkeypatch.setattr(ct, "_project_source_path", lambda pid: source_path)
    monkeypatch.setattr(cm, "configured_project_dir", lambda pid: override_dir)


def _record_drive_evidence(scores_db: str, task_id: str, session_id: str,
                            step: str = "write_failing_tests") -> None:
    """Write a real agent_runs row for (task_id, session_id) — the same
    shape ConductorService._record_agent_run writes on every advance_task —
    i.e. objective evidence this task was actually DRIVEN under this
    session, not merely linked to it."""
    from prism_service.services import agent_runs_data

    now = time.time()
    agent_runs_data.upsert_agent_run(scores_db, {
        "run_id": f"{task_id}:{step}",
        "workflow_name": "implement",
        "task_id": task_id,
        "session_id": session_id,
        "agent_id": f"{task_id}:{step}",
        "role": "qa",
        "step": step,
        "model": "test",
        "started_at": str(now - 60),
        "ended_at": str(now),
        "duration_ms": 60000,
        "tokens": 100,
        "tool_uses": 3,
        "ok": True,
        "gate_state": "none",
        "verdict_summary": "test-authored drive evidence",
        "evidence_ref": "",
    })


# ----------------------------------------------------------------------
# The exact regression: one session, two unrelated tasks.
# ----------------------------------------------------------------------

def test_shared_session_linked_to_two_tasks_scores_distinctly(tmp_path, monkeypatch):
    """Core oracle: session `sid` is linked to BOTH task_driven and
    task_bystander. task_driven has real agent_runs drive evidence under
    `sid`; task_bystander has only a bare task_sessions link (e.g. picked up
    by a locate/context sweep) with NO drive evidence. The two tiles must NOT
    render identical non-zero tok_total — this reproduces daffe20c painting
    52,885,559 on both 170991cc and 2419bff2."""
    task_svc, cond, scores_db = _conductor(tmp_path)
    sid = "daffe20c-dead-beef-dead-beefdeadbeef"
    d = tmp_path / "claudedir"
    now = time.time()
    _write_transcript(
        d / f"{sid}.jsonl",
        [(now - 40, 5_000), (now - 25, 6_000), (now - 5, 7_000)],
        sid=sid,
    )
    _patch_resolvers(monkeypatch, source_path=str(tmp_path / "src"), override_dir=str(d))

    task_driven = task_svc.create(title="actually drove this session")
    task_bystander = task_svc.create(title="unrelated task, session merely linked")

    # Both tasks are linked to the SAME session (the contested case).
    task_svc.link_session(task_driven.id, sid)
    task_svc.link_session(task_bystander.id, sid)
    task_svc.update(task_driven.id, status="in_progress")
    task_svc.update(task_bystander.id, status="in_progress")

    # Only task_driven has objective drive evidence (agent_runs) under sid.
    _record_drive_evidence(scores_db, task_driven.id, sid)

    pp_driven = cond.phase_progress(task_driven.id)
    pp_bystander = cond.phase_progress(task_bystander.id)

    assert pp_driven["tokens_task_total"] > 0, (
        "the task that actually drove the session must keep reporting real "
        f"burn, got {pp_driven['tokens_task_total']}"
    )
    assert pp_bystander["tokens_task_total"] == 0, (
        "a task with no drive evidence under a shared session must NOT "
        f"inherit its burn — got {pp_bystander['tokens_task_total']} "
        f"(driven task read {pp_driven['tokens_task_total']})"
    )
    assert pp_driven["tokens_task_total"] != pp_bystander["tokens_task_total"], (
        "reproduces the exact regression: two unrelated tasks sharing one "
        "session must never render identical non-zero tok_total "
        f"({pp_driven['tokens_task_total']} == {pp_bystander['tokens_task_total']})"
    )
    assert pp_bystander["turns"] == 0, (
        f"bystander task must not inherit the driven task's turn count, "
        f"got {pp_bystander['turns']}"
    )
    assert pp_bystander["token_turns"] == [], (
        "bystander task must not plot the driven task's per-turn burn graph"
    )
    # AC-2 (approved plan_doc section 8): a bare link with no drive evidence
    # is EXPLICITLY labeled, never silently rendered as an ordinary 'linked'
    # zero (which would read identically to a task that just hasn't started).
    assert pp_bystander["tokens_source"] == "unattributed", (
        f"a contested session with no drive evidence for this task must "
        f"report tokens_source=='unattributed', got "
        f"{pp_bystander['tokens_source']!r}"
    )
    assert pp_driven["tokens_source"] == "linked", (
        f"the actively-driven task keeps the ordinary 'linked' source, got "
        f"{pp_driven['tokens_source']!r}"
    )


def test_bystander_task_reads_zero_tok_s_not_borrowed_rate(tmp_path, monkeypatch):
    """The per-turn burn SERIES (tok/s graph) must be empty for the
    bystander, not merely the cumulative total — a non-empty token_turns with
    a zeroed total would still paint a false 'work getting done' graph."""
    task_svc, cond, scores_db = _conductor(tmp_path)
    sid = "shared-sid-0000-0000-000000000000"
    d = tmp_path / "claudedir"
    now = time.time()
    _write_transcript(
        d / f"{sid}.jsonl",
        [(now - 30, 1_000), (now - 10, 2_000)],
        sid=sid,
    )
    _patch_resolvers(monkeypatch, source_path=str(tmp_path / "src"), override_dir=str(d))

    owner = task_svc.create(title="owns the session")
    stranger = task_svc.create(title="stranger, linked but never drove it")
    task_svc.link_session(owner.id, sid)
    task_svc.link_session(stranger.id, sid)
    task_svc.update(owner.id, status="in_progress")
    task_svc.update(stranger.id, status="in_progress")
    _record_drive_evidence(scores_db, owner.id, sid)

    pp_stranger = cond.phase_progress(stranger.id)
    assert pp_stranger["token_turns"] == [], (
        f"stranger's burn graph must be empty, got {len(pp_stranger['token_turns'])} points"
    )
    assert pp_stranger["tokens_since_step"] == 0, (
        f"stranger's step readout must be 0, got {pp_stranger['tokens_since_step']}"
    )


# ----------------------------------------------------------------------
# Regression guard: epic/child aggregation must still work (owner
# 2026-08-06) — a child that genuinely, exclusively drove its own session
# must still roll its burn up into the parent's tile.
# ----------------------------------------------------------------------

def test_epic_rollup_still_sums_a_genuinely_driven_childs_own_session(tmp_path, monkeypatch):
    task_svc, cond, scores_db = _conductor(tmp_path)
    sid = "child-own-sid-1111-1111-111111111111"
    d = tmp_path / "claudedir"
    now = time.time()
    _write_transcript(
        d / f"{sid}.jsonl",
        [(now - 20, 3_000), (now - 5, 4_000)],
        sid=sid,
    )
    _patch_resolvers(monkeypatch, source_path=str(tmp_path / "src"), override_dir=str(d))

    parent = task_svc.create(title="epic")
    child = task_svc.create(title="child lane", parent_id=parent.id)
    # The child's session is UNCONTESTED (linked only to the child) and the
    # child genuinely drove it.
    task_svc.link_session(child.id, sid)
    task_svc.update(child.id, status="in_progress")
    _record_drive_evidence(scores_db, child.id, sid)

    pp_parent = cond.phase_progress(parent.id)
    assert pp_parent["tokens_task_total"] > 0, (
        "an epic with an actively-driven child lane must still show "
        f"non-zero burn — got {pp_parent['tokens_task_total']}"
    )
