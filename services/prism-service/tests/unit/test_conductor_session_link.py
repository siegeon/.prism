"""Conductor transitions must link a session into task_sessions.

Regression for the ed7292bd symptom: a task driven fully through the
conductor showed 0 attached sessions. Root cause was the MCP layer not
threading a session into advance_task/gate_decide (conductor_gate did not
even expose session_id), so nothing stamped task_sessions.

These pin the SERVICE contract the handler now always satisfies (the MCP
handlers fall back to the request handle when no session_id is passed,
mirroring task_link_session): when a session_id reaches advance_task or
gate_decide, it MUST be linked and readable via sessions_for_task.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores = str(tmp_path / "scores.db")
    # TaskService shares the SAME scores.db so its link writer targets the
    # file ConductorService reads/writes.
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores)
    cond = ConductorService(scores, enable_engine=False, task_svc=task_svc)
    return task_svc, cond, scores


def _linked(scores_path, task_id):
    # Read the task_sessions link rows directly. (sessions_for_task LEFT JOINs
    # session_outcomes, which a bare test scores.db lacks; we only care that
    # the conductor stamped the link.)
    import sqlite3
    conn = sqlite3.connect(scores_path)
    try:
        rows = conn.execute(
            "SELECT session_id FROM task_sessions WHERE task_id=?", (task_id,)
        ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()


def test_advance_task_links_session(tmp_path):
    task_svc, cond, scores = _services(tmp_path)
    t = task_svc.create(title="link on advance")
    cond.advance_task(t.id, validation="entering", session_id="sess-advance")
    assert "sess-advance" in _linked(scores, t.id)


def test_gate_decide_links_session(tmp_path):
    """The terminal-gate close-out path (conductor_gate) must record the
    session that resolved the gate — this is the exact path that left
    ed7292bd with 0 sessions."""
    task_svc, cond, scores = _services(tmp_path)
    t = task_svc.create(title="link on gate")

    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this unrelated session-link walk can leave review_previous_notes.
    task_svc.update(t.id, premise_notes=(
        "## Premises\n- fixture walk exercising session linking, not a "
        "real premise claim - UNVERIFIED\n"))

    # Drive to red_gate, clearing the earlier story/plan gates (task
    # 8579d49e) with a plain approve (bare conductor = trust-caller).
    guard = 0
    while task_svc.get(t.id).workflow_step != "red_gate" and guard < 20:
        if task_svc.get(t.id).gate_state == "pending":
            cond.gate_decide(t.id, "approve", reason="walk intermediate")
        else:
            cond.advance_task(t.id, validation="step",
                              session_id="sess-advance")
        guard += 1
    assert task_svc.get(t.id).workflow_step == "red_gate"

    cond.gate_decide(
        t.id, "approve", reason="override close-out",
        override=True, session_id="sess-gate",
    )
    assert "sess-gate" in _linked(scores, t.id)
