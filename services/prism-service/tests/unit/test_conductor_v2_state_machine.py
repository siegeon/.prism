"""Conductor v2 — per-task workflow state machine (issue #79 [1/4]).

Backend-only tests: schema migration, advance_task, gate_decide, and the
task_history audit trail. No MCP or UI surface is exercised here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"),
        enable_engine=False,
        task_svc=task_svc,
    )
    return task_svc, cond


def _workflow():
    from prism_service.models.workflow import WORKFLOW_STEPS

    return WORKFLOW_STEPS


# ----------------------------------------------------------------------
# Schema + dataclass
# ----------------------------------------------------------------------


def test_new_task_defaults_for_workflow_columns(tmp_path):
    task_svc, _ = _services(tmp_path)
    t = task_svc.create(title="Add Conductor v2 state machine")
    assert t.workflow_step == ""
    assert t.gate_state == "none"
    assert t.gate_reason == ""

    # Round-trip through the DB (defaults applied by SQLite, not the
    # dataclass) — the new columns must read back identically.
    fetched = task_svc.get(t.id)
    assert fetched is not None
    assert fetched.workflow_step == ""
    assert fetched.gate_state == "none"
    assert fetched.gate_reason == ""


def test_migration_adds_columns_to_pre_existing_tasks_db(tmp_path):
    """Open the DB once without the Conductor v2 columns, then re-open
    with TaskService — the ALTER TABLE migration must backfill them."""
    import sqlite3

    db_path = tmp_path / "tasks.db"
    pre = sqlite3.connect(db_path)
    pre.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            priority INTEGER DEFAULT 0,
            story_file TEXT DEFAULT '',
            assigned_agent TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT DEFAULT '',
            completed_at TEXT DEFAULT '',
            blocked_reason TEXT DEFAULT '',
            dependencies TEXT DEFAULT '[]',
            tags TEXT DEFAULT '[]'
        );
        CREATE TABLE task_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            actor TEXT DEFAULT '',
            action TEXT NOT NULL,
            details TEXT DEFAULT '',
            timestamp TEXT NOT NULL
        );
        INSERT INTO tasks (id, title, created_at)
        VALUES ('legacy-1', 'old task', '2026-01-01T00:00:00+00:00');
        """
    )
    pre.commit()
    pre.close()

    from prism_service.services.task_service import TaskService

    svc = TaskService(str(db_path))
    cols = {row[1] for row in svc._db.execute("PRAGMA table_info(tasks)").fetchall()}
    for c in ("workflow_step", "gate_state", "gate_reason"):
        assert c in cols, f"{c} was not added by migration"

    legacy = svc.get("legacy-1")
    assert legacy is not None
    # ALTER TABLE ... DEFAULT applies to NEW inserts only; existing rows
    # read back NULL on the new columns, and _row_to_task must coerce
    # those NULLs to the documented defaults.
    assert legacy.workflow_step == ""
    assert legacy.gate_state == "none"
    assert legacy.gate_reason == ""


# ----------------------------------------------------------------------
# advance_task
# ----------------------------------------------------------------------


def test_advance_from_no_step_lands_on_first_step(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="First task")

    result = cond.advance_task(t.id)

    assert result["ok"] is True
    assert result["from_step"] == ""
    assert result["to_step"] == _workflow()[0]["id"]
    assert result["gate_state"] == "none"

    refreshed = task_svc.get(t.id)
    assert refreshed.workflow_step == _workflow()[0]["id"]
    assert refreshed.gate_state == "none"


def test_advance_into_gate_sets_gate_state_pending(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Walks into a gate")

    # Walk up to (and including) the first gate.
    steps = _workflow()
    gate_index = next(i for i, s in enumerate(steps) if s["type"] == "gate")
    for _ in range(gate_index + 1):
        cond.advance_task(t.id)

    refreshed = task_svc.get(t.id)
    assert refreshed.workflow_step == steps[gate_index]["id"]
    assert refreshed.gate_state == "pending"


def test_advance_refused_when_gate_pending(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Stuck at gate")
    steps = _workflow()
    gate_index = next(i for i, s in enumerate(steps) if s["type"] == "gate")
    for _ in range(gate_index + 1):
        cond.advance_task(t.id)

    refused = cond.advance_task(t.id)

    assert refused["ok"] is False
    assert "pending" in refused["reason"]
    assert refused["from_step"] == steps[gate_index]["id"]
    assert refused["to_step"] == steps[gate_index]["id"]

    # Task state is unchanged.
    refreshed = task_svc.get(t.id)
    assert refreshed.workflow_step == steps[gate_index]["id"]
    assert refreshed.gate_state == "pending"


def test_advance_past_gate_when_gate_state_passed(tmp_path):
    """gate_decide('approve') auto-advances; verify the result is the
    step immediately after the gate."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Approve and roll forward")
    steps = _workflow()
    gate_index = next(i for i, s in enumerate(steps) if s["type"] == "gate")
    for _ in range(gate_index + 1):
        cond.advance_task(t.id)

    decided = cond.gate_decide(
        t.id, action="approve",
        reason="test: approve and advance; pytest -q -> 1 failed"
    )

    assert decided["ok"] is True
    assert decided["gate_state"] == "passed"
    assert decided["to_step"] == steps[gate_index + 1]["id"]
    refreshed = task_svc.get(t.id)
    # After the auto-advance, workflow_step has moved off the gate and
    # gate_state has been cleared back to 'none' (we left the gate).
    assert refreshed.workflow_step == steps[gate_index + 1]["id"]
    assert refreshed.gate_state == "none"


def test_advance_at_final_step_returns_refusal(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Walks to the end")
    steps = _workflow()

    # Walk to the end. After each step, if we landed on a gate we
    # approve it so the next advance_task can proceed. Final gate gets
    # approved too — auto-advance from approve will refuse (no next
    # step), which is the situation we want to assert on below.
    safety = len(steps) * 3
    while safety > 0:
        safety -= 1
        snapshot = task_svc.get(t.id)
        if snapshot.gate_state == "pending":
            cond.gate_decide(
                t.id, action="approve", reason="test: walk to end"
            )
            continue
        if snapshot.workflow_step == steps[-1]["id"]:
            break
        cond.advance_task(t.id)

    refreshed = task_svc.get(t.id)
    assert refreshed.workflow_step == steps[-1]["id"]

    over = cond.advance_task(t.id)
    assert over["ok"] is False
    assert "final" in over["reason"]


def test_advance_unknown_task_is_refused(tmp_path):
    _, cond = _services(tmp_path)
    result = cond.advance_task("not-a-real-id")
    assert result["ok"] is False
    assert "unknown" in result["reason"]


# ----------------------------------------------------------------------
# gate_decide
# ----------------------------------------------------------------------


def test_gate_decide_rejects_unknown_action(tmp_path):
    _, cond = _services(tmp_path)
    result = cond.gate_decide("any-id", action="maybe")
    assert result["ok"] is False
    assert "unknown action" in result["reason"]


def test_gate_decide_refused_when_not_on_gate(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Pre-gate task")
    cond.advance_task(t.id)  # arrives on step[0], an 'agent' type

    result = cond.gate_decide(t.id, action="approve")

    assert result["ok"] is False
    assert "not currently on a gate" in result["reason"]


def test_gate_decide_reject_does_not_auto_advance(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Reject me")
    steps = _workflow()
    gate_index = next(i for i, s in enumerate(steps) if s["type"] == "gate")
    for _ in range(gate_index + 1):
        cond.advance_task(t.id)

    result = cond.gate_decide(t.id, action="reject", reason="needs more eyes")

    assert result["ok"] is True
    assert result["gate_state"] == "failed"
    refreshed = task_svc.get(t.id)
    assert refreshed.workflow_step == steps[gate_index]["id"]
    assert refreshed.gate_state == "failed"
    assert refreshed.gate_reason == "needs more eyes"


def test_gate_decide_refuses_when_state_not_pending(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Already rejected")
    steps = _workflow()
    gate_index = next(i for i, s in enumerate(steps) if s["type"] == "gate")
    for _ in range(gate_index + 1):
        cond.advance_task(t.id)
    cond.gate_decide(t.id, action="reject", reason="nope")

    # After reject the gate is 'failed'. The new contract (v6.0.31) says
    # recovery requires action='approve' AND override=True; a plain
    # approve without override must still be refused.
    repeat = cond.gate_decide(t.id, action="approve", reason="retry")
    assert repeat["ok"] is False
    assert "failed" in repeat["reason"]
    assert "override=True" in repeat["reason"]


# ----------------------------------------------------------------------
# task_history audit trail
# ----------------------------------------------------------------------


def test_task_history_captures_every_transition(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Audit trail")
    steps = _workflow()
    gate_index = next(i for i, s in enumerate(steps) if s["type"] == "gate")

    for _ in range(gate_index + 1):
        cond.advance_task(t.id)
    cond.gate_decide(
        t.id, action="approve",
        reason="test: audit trail; pytest -q -> 1 failed"
    )  # auto-advances past gate

    actions = [h.action for h in task_svc.history(t.id)]
    # Expected: created + (gate_index + 1) advance_task + gate_decide +
    # one more advance_task from the auto-advance.
    assert actions[0] == "created"
    assert actions.count("advance_task") == gate_index + 2
    assert actions.count("gate_decide") == 1


def test_task_history_records_validation_on_advance(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Carries validation token")

    cond.advance_task(t.id, validation="plan_coverage")
    rows = task_svc.history(t.id)
    advance_rows = [r for r in rows if r.action == "advance_task"]
    assert advance_rows, "advance_task must produce a task_history row"
    assert "validation=plan_coverage" in advance_rows[-1].details


# ----------------------------------------------------------------------
# Failed gates must latch the state machine (task baab6b51)
# ----------------------------------------------------------------------


def test_advance_refused_when_gate_failed(tmp_path):
    """A FAILED gate must block advance_task just like a pending one.

    Live-probe finding (task 9f20b605): advance_task only checked
    gate_state == 'pending', so a task sitting on a REJECTED gate could
    legally be advanced past it with no override and no re-decision.
    """
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Failed gate latches")
    steps = _workflow()
    gate_index = next(i for i, s in enumerate(steps) if s["type"] == "gate")
    for _ in range(gate_index + 1):
        cond.advance_task(t.id)
    cond.gate_decide(t.id, action="reject", reason="verifier said no")

    refused = cond.advance_task(t.id)

    assert refused["ok"] is False
    assert "failed" in refused["reason"]
    assert refused["from_step"] == steps[gate_index]["id"]
    assert refused["to_step"] == steps[gate_index]["id"]

    # Task state untouched: still latched on the failed gate, with the
    # rejection reason preserved.
    refreshed = task_svc.get(t.id)
    assert refreshed.workflow_step == steps[gate_index]["id"]
    assert refreshed.gate_state == "failed"
    assert refreshed.gate_reason == "verifier said no"


def test_failed_gate_recovers_via_override_then_advances(tmp_path):
    """The latch releases ONLY through the gate_decide recovery path
    (approve + override=True, distinct actor); afterwards advance_task
    proceeds normally."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Recover failed gate")
    steps = _workflow()
    gate_index = next(i for i, s in enumerate(steps) if s["type"] == "gate")
    for _ in range(gate_index + 1):
        cond.advance_task(t.id)
    cond.gate_decide(t.id, action="reject", reason="nope")

    # Latched: advance must be refused while the gate is failed.
    assert cond.advance_task(t.id)["ok"] is False

    recovered = cond.gate_decide(
        t.id, action="approve", override=True,
        actor="independent-verifier",
        reason="remediated; pytest -q green",
    )
    assert recovered["ok"] is True
    assert recovered["gate_state"] == "passed"

    refreshed = task_svc.get(t.id)
    assert refreshed.workflow_step == steps[gate_index + 1]["id"]
    onward = cond.advance_task(t.id)
    assert onward["ok"] is True
