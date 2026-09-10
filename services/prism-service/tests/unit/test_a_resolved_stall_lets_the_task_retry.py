"""A fixed defect lets its stalled task retry (task 1ecbd866-142b-4c58-a59b-9cdc295a42d3).

A manual status transition out of blocked is a fresh mandate to retry, just like
a gate rejection (REWIND_ACTION). When an operator moves a task from blocked back
to in_progress, _stall_count must reset the budget so the next runner tick does
not route straight to _handle_stall again.

The history row recording the status flip carries an actor (the user or system
that changed it), and _stall_count should treat it as a budget boundary exactly
as it treats REWIND_ACTION — counting only attempts AFTER the most recent
boundary marker.
"""

from __future__ import annotations

import types

import pytest

from prism_service.services import task_runner as tr


class _StallTask:
    id = "t-1"
    verify: list = []
    completion_proof = ""
    proof_type = "test"
    oracle = ""
    likely_misfire = ""
    priority = 10
    tags: list = []
    status = "blocked"
    gate_state = "none"


class _StallSvc:
    def __init__(self):
        self.updates = []
        self.hist = []

    def get(self, _tid):
        return _StallTask()

    def list(self, **_kw):
        return []

    def update(self, tid, **kw):
        self.updates.append((tid, kw))

    def record_history(self, tid, **kw):
        self.hist.append(types.SimpleNamespace(**kw))

    def history(self, _tid):
        return self.hist


def test_an_operator_reset_lets_the_step_run_again():
    """When an operator manually moves a task from blocked to in_progress,
    the stall budget resets and the step can run again.

    Simulates the sequence: step stalls 3 times (budget exhausted), task goes
    blocked, operator manually clicks "in_progress" on the task page (which
    records a status transition history row with an actor), next runner tick
    should run the step again (stall count should be 0 for the new pass).
    """
    svc = _StallSvc()

    # Simulate three failed attempts at draft_story
    for i in range(3):
        svc.record_history(
            "t-1",
            action=tr.ATTEMPT_ACTION,
            details=f"step=draft_story; advanced=false",
            actor="prism-task-runner")

    # Simulate operator moving task from blocked to in_progress
    # This should be a boundary marker for the stall count
    svc.record_history(
        "t-1",
        action="status_change",
        details="status: blocked -> in_progress",
        actor="owner")

    # After the status change, stall count should be 0
    count = tr._stall_count(svc, "t-1", "draft_story")

    assert count == 0, (
        f"stall count should reset after operator status change, but got {count}")


def test_an_unresolved_stall_still_blocks():
    """When a task stays in blocked (no status change), it still blocks
    after three attempts.

    This is the guard: confirm that the retry reset is specific to status
    changes, not a general weakening of the stall threshold.
    """
    svc = _StallSvc()

    # Simulate three failed attempts at draft_story
    for i in range(3):
        svc.record_history(
            "t-1",
            action=tr.ATTEMPT_ACTION,
            details=f"step=draft_story; advanced=false",
            actor="prism-task-runner")

    # NO status change — the task stays blocked

    # Stall count should still be 3 (still at the limit)
    count = tr._stall_count(svc, "t-1", "draft_story")

    assert count == 3, (
        f"unresolved stall should keep stall count at {tr.STALL_ATTEMPTS}, got {count}")
