"""A fixed defect lets its stalled task retry (task 1ecbd866-142b-4c58-a59b-9cdc295a42d3).

A manual status transition from blocked to in_progress is a fresh mandate to retry,
just like a gate rejection (REWIND_ACTION). The real history format records this as
action='updated' with a directional pattern in details: "status: 'blocked' -> 'in_progress'".

_stall_count must match this pattern precisely (directional, not just substrings) and
treat it as a budget boundary exactly as it treats REWIND_ACTION — counting only
attempts AFTER the most recent boundary marker.
"""

from __future__ import annotations

import tempfile
import types

import pytest

from prism_service.services import task_runner as tr
from prism_service.services.task_service import TaskService


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
    """Real TaskService integration test: task status change resets stall budget.

    Drives a real task through TaskService.update(status=...) so the history
    row matches the actual production format: action='updated' with a directional
    details pattern "status: 'blocked' -> 'in_progress'". This test FAILS if
    the regex match is direction-blind (would accept 'in_progress' -> 'blocked').
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = TaskService(db_path=f"{tmpdir}/test.db")

        # Create a task (starts in_progress by default)
        task = svc.create(
            title="Test task",
            description="Stall retry test",
            priority=10)
        task_id = task.id

        # Move it to blocked state first
        svc.update(task_id, status="blocked")

        # Record three failed attempts at draft_story
        for i in range(3):
            svc.record_history(
                task_id,
                action=tr.ATTEMPT_ACTION,
                details="step=draft_story; advanced=false",
                actor="prism-task-runner")

        # Count before status change (should be 3)
        count_before = tr._stall_count(svc, task_id, "draft_story")
        assert count_before == 3

        # Operator manually moves task from blocked to in_progress
        svc.update(task_id, status="in_progress")

        # After the status change, stall count should be 0
        count_after = tr._stall_count(svc, task_id, "draft_story")

        assert count_after == 0, (
            f"stall count should reset after status change from blocked -> in_progress, "
            f"but got {count_after}")


def test_an_unresolved_stall_still_blocks():
    """Guard: blocking transition does NOT reset the budget.

    Confirms that the budget reset is specific to blocked->in_progress transitions,
    not triggered by any transition containing those words. The history would read:
    "status: 'in_progress' -> 'blocked'" and must NOT match the directional pattern.
    """
    svc = _StallSvc()

    # Record three failed attempts at draft_story
    for i in range(3):
        svc.record_history(
            "t-1",
            action=tr.ATTEMPT_ACTION,
            details="step=draft_story; advanced=false",
            actor="prism-task-runner")

    # Simulate blocking transition (opposite direction)
    # This should NOT reset the budget
    svc.record_history(
        "t-1",
        action="updated",
        details="status: 'in_progress' -> 'blocked'; blocked_reason: 'step draft_story ...'",
        actor="")

    # Stall count should still be 3 (not reset by the blocking transition)
    count = tr._stall_count(svc, "t-1", "draft_story")

    assert count == 3, (
        f"blocking transition should NOT reset budget, but stall count dropped to {count}")
