"""Gate evaluation can send a task BACK to implement (task 8582921d).

Trace: owner 2026-08-19 — after a landing moved 9a51e670's tree its green
receipt went STALE; `adjudicate_green_gate` abstained every sweep and the
only remedy was a human re-mint. The FSM was forward-only for machines.
These tests pin the backward edge the plan_doc designs:
  _evaluate_green_gate_rewind(task, tree_sha, tried, refusal, remint_attempted)
  _auto_rewind(task_id, target_step, reason, evidence_ref)
  _consecutive_auto_rewinds(task_id)     MAX_AUTO_REWINDS
AC-2 red-at-tree -> implement_tasks | AC-3 stale -> verify_green_state
AC-4 bounded      | AC-5 human reject never crossed | AC-6 seat + evidence.
"""
from types import SimpleNamespace

from prism_service.services import conductor_service as cs
from prism_service.services.conductor_service import (ADJUDICATOR_SEAT,
                                                       ConductorService)

OLD, NEW = "a" * 40, "b" * 40
STALE = (f"green receipt is STALE — it was run at tree={OLD} but the task "
         f"is now at tree={NEW}")
RED = "latest receipt FAILED: tests/unit/test_pinned.py::test_x"


class FakeTaskService:
    def __init__(self, task):
        self.task, self.rows = task, []

    def get(self, task_id):
        return self.task

    def update(self, task_id, **fields):
        for k, v in fields.items():
            setattr(self.task, k, v)
        return self.task

    def record_history(self, task_id, action, **kw):
        self.rows.append(SimpleNamespace(action=action, actor=kw.get("actor"),
                                         model=kw.get("model"),
                                         details=kw.get("details", ""),
                                         reason=kw.get("reason", "")))

    def history(self, task_id):
        return list(self.rows)


def _svc():
    task = SimpleNamespace(id="t1", workflow_step="green_gate",
                           gate_state="pending", gate_reason="",
                           blocked_reason="", verify=[
                               "services/prism-service/tests/unit/test_pinned.py"])
    svc = ConductorService.__new__(ConductorService)
    svc._task_svc = FakeTaskService(task)
    svc._project_name = "default"
    return svc, task, svc._task_svc


def _rewinds(fake):
    return [r for r in fake.rows if r.action == "auto_rewind"]


def test_stale_receipt_triggers_remint_or_rewind():
    """AC-3: stale receipt, no confirming re-mint this sweep -> the task
    is sent back to verify_green_state with the staleness as its work order."""
    svc, task, fake = _svc()
    svc._evaluate_green_gate_rewind(task, NEW, False, STALE, False)
    assert task.workflow_step == "verify_green_state"
    assert task.gate_state == "none"
    assert OLD[:12] in task.gate_reason and NEW[:12] in task.gate_reason
    row = _rewinds(fake)[-1]
    assert row.actor == ADJUDICATOR_SEAT and row.model == "machine"
    assert NEW[:12] in row.details


def test_confirmed_red_at_current_tree_rewinds_to_implement():
    """AC-2: the oracle RAN at the current tree and failed -> implement_tasks,
    failure text carried forward verbatim, history preserved."""
    svc, task, fake = _svc()
    fake.record_history("t1", "advance_task", actor="prism-task-runner")
    svc._evaluate_green_gate_rewind(task, NEW, True, RED, False)
    assert task.workflow_step == "implement_tasks"
    assert "test_pinned.py::test_x" in task.gate_reason
    assert fake.rows[0].action == "advance_task"  # nothing rewritten
    assert "test_pinned.py::test_x" in _rewinds(fake)[-1].details


def test_fresh_remint_that_is_still_red_is_a_regression_not_staleness():
    svc, task, _ = _svc()
    svc._evaluate_green_gate_rewind(task, NEW, False, RED, True)
    assert task.workflow_step == "implement_tasks"


def test_human_reject_is_never_crossed():
    """AC-5 / stop_if: newest gate_decide on green_gate is a HUMAN reject
    with no forward row after it -> no state write, no rewind row."""
    svc, task, fake = _svc()
    fake.record_history("t1", "gate_decide", actor="owner", model="human",
                        details="green_gate reject: not what I asked for")
    task.gate_state = "failed"
    svc._evaluate_green_gate_rewind(task, NEW, False, STALE, False)
    assert task.workflow_step == "green_gate"
    assert task.gate_state == "failed"
    assert _rewinds(fake) == []


def test_rewind_budget_is_bounded_and_parks_loudly():
    """AC-4 / stop_if: after MAX_AUTO_REWINDS consecutive auto rewinds the
    next eligible sweep does NOT move the step; it parks with a visible reason
    under a DISTINCT action that does not count toward the budget."""
    svc, task, fake = _svc()
    n = cs.MAX_AUTO_REWINDS
    assert 1 <= n <= 5
    for _ in range(n):
        fake.record_history("t1", "auto_rewind", actor=ADJUDICATOR_SEAT,
                            model="machine", details="green_gate -> ...")
    assert svc._consecutive_auto_rewinds("t1") == n
    svc._evaluate_green_gate_rewind(task, NEW, False, STALE, False)
    assert task.workflow_step == "green_gate"
    assert task.gate_state == "pending"
    assert str(n) in task.gate_reason and task.blocked_reason
    assert fake.rows[-1].action == "auto_rewind_exhausted"
    assert len(_rewinds(fake)) == n


def test_counter_stops_at_first_forward_or_human_row():
    svc, _, fake = _svc()
    fake.record_history("t1", "auto_rewind", actor=ADJUDICATOR_SEAT)
    fake.record_history("t1", "advance_task", actor="prism-task-runner")
    fake.record_history("t1", "auto_rewind", actor=ADJUDICATOR_SEAT)
    assert svc._consecutive_auto_rewinds("t1") == 1


def test_auto_rewind_row_carries_seat_and_evidence():
    """AC-6: never blank, never 'owner'; evidence ref in the row."""
    svc, task, fake = _svc()
    svc._auto_rewind("t1", "implement_tasks", RED, "evidence=job-42")
    row = _rewinds(fake)[-1]
    assert row.actor == ADJUDICATOR_SEAT and row.actor != "owner"
    assert "job-42" in row.details and "implement_tasks" in row.details
    assert task.workflow_step == "implement_tasks" and task.gate_state == "none"
