"""A refused red gate rewinds to its producing step - task 1bcb2b24.

red_gate is the ONE gate a human must never be asked to clear (the owner
rule that red belongs to the machine seat). It also had no rewind path,
while plan_gate/story_gate had plan_rewind and green_gate had green_rewind.
So a red_gate that refused was unreachable from BOTH sides and the task sat
pending for ever. Observed live on task 1bcb2b24 on 2026-09-08.

  AC-1  a refused red_gate returns the drive to write_failing_tests.
  AC-2  a red_gate an actor already decided is never rewound.
  AC-3  a spent rewind budget parks and names itself.
  AC-4  "cannot measure" is not "refused" - the 7.13.190 green_rewind bug.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from prism_service.services import plan_rewind


_NOT_RED = ("NOT red: the spec's tests PASS at the red-step commit "
            "c43012b7afa3 (pytest_ids: tests/unit/test_x.py -> rc=0)")


class _FakeTaskSvc:
    """Records update()/record_history() the way plan_rewind calls them."""

    def __init__(self):
        self.updates: list[dict] = []
        self.rows: list[SimpleNamespace] = []

    def update(self, task_id, **kw):
        self.updates.append({"task_id": task_id, **kw})

    def record_history(self, task_id, action="", details="", actor=""):
        self.rows.append(SimpleNamespace(
            task_id=task_id, action=action, details=details, actor=actor))

    def history(self, task_id):
        return list(self.rows)


def _task(gate_state="pending", gate_reason=_NOT_RED, step="red_gate"):
    return SimpleNamespace(id="t-red", workflow_step=step,
                           gate_state=gate_state, gate_reason=gate_reason)


@pytest.fixture()
def ctx():
    return SimpleNamespace(task_svc=_FakeTaskSvc(), conductor_svc=None)


def test_a_refused_red_gate_returns_to_write_failing_tests(ctx):
    """AC-1: the whole point. Before this slice _STEP_BEFORE had no
    red_gate key, so maybe_rewind returned None and nothing moved."""
    got = plan_rewind.maybe_rewind(ctx, _task(), "prism")

    assert got is not None, "red_gate must be rewindable"
    assert got["ok"] is True, got
    assert got["from_step"] == "red_gate"
    assert got["to_step"] == "write_failing_tests"

    upd = ctx.task_svc.updates[-1]
    assert upd["workflow_step"] == "write_failing_tests"
    # A rewind lands on an AGENT step, which has no gate. Leaving "pending"
    # here creates a row is_open_gate_step cannot see.
    assert upd["gate_state"] == "none"
    assert upd["status"] == "in_progress"
    assert "NOT red" in upd["gate_reason"], "quote the seat's own words"

    row = ctx.task_svc.rows[-1]
    assert row.action == plan_rewind.REWIND_ACTION
    assert "red_gate -> write_failing_tests" in row.details


def test_a_decided_red_gate_is_never_rewound(ctx):
    """AC-2: undoing a decision an actor already made would erase a real
    judgement. Only a PENDING gate rewinds."""
    for decided in ("passed", "failed", "none"):
        assert plan_rewind.maybe_rewind(
            ctx, _task(gate_state=decided), "prism") is None, decided
    assert ctx.task_svc.updates == []


def test_a_seat_that_could_not_measure_is_not_a_refusal(ctx):
    """AC-4: "cannot judge" is NOT "refused" - the distinction green_rewind
    shipped wrong in 7.13.190 by testing a boolean false for every
    non-pass. A reason that does not say NOT red must not burn a rewind."""
    quiet = _task(gate_reason="awaiting a trusted run of the pinned suite")
    got = plan_rewind.maybe_rewind(ctx, quiet, "prism")
    assert got is not None and got.get("ok") is not True, got
    assert got.get("inconclusive") is True, got
    assert not any(u.get("workflow_step") for u in ctx.task_svc.updates)


def test_a_spent_budget_parks_and_names_itself(ctx, monkeypatch):
    """AC-3: the budget must bound the retry. Each red rewind re-runs a
    step agent on a real USD budget, so an unbounded loop is expensive as
    well as useless."""
    monkeypatch.setattr(plan_rewind, "rewind_budget", lambda project: 2)
    task = _task()

    for attempt in (1, 2):
        got = plan_rewind.maybe_rewind(ctx, task, "prism")
        assert got["ok"] is True, (attempt, got)

    spent = plan_rewind.maybe_rewind(ctx, task, "prism")
    assert spent["ok"] is False and spent["parked"] is True, spent
    reason = ctx.task_svc.updates[-1]["gate_reason"]
    assert "budget" in reason.lower() and "2" in reason, reason


def test_the_red_budget_is_its_own(ctx, monkeypatch):
    """A plan rewind must not spend the red budget. rewind_count scopes by
    from-step for exactly this reason."""
    monkeypatch.setattr(plan_rewind, "rewind_budget", lambda project: 1)
    ctx.task_svc.record_history(
        "t-red", action=plan_rewind.REWIND_ACTION,
        details="plan_gate -> verify_plan; attempt=1/3")

    got = plan_rewind.maybe_rewind(ctx, _task(), "prism")
    assert got["ok"] is True, "a plan rewind must not spend red's budget"


def test_the_adjudicator_offers_red_gate_to_the_rewind():
    """The map alone is inert: gate_adjudicator must actually call
    plan_rewind for red_gate, or the row still never moves."""
    import inspect
    from prism_service.services import gate_adjudicator
    src = inspect.getsource(gate_adjudicator)
    idx = src.find("plan_rewind")
    assert idx > 0
    window = src[max(0, idx - 600):idx]
    assert '"red_gate"' in window, (
        "gate_adjudicator must route red_gate into plan_rewind.maybe_rewind")
