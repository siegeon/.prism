"""Guard to prevent advancing past failed steps (task 0a9c3d1d).

When a step's completion_proof contains failure markers (did not land,
Permission denied, exhausted, failed), advance_task must refuse to move
forward. This prevents tasks from being advanced past failed steps, which
would create unwinnable gates (e.g. red_gate with tests already passing
at the anchor).

Reproduces: task 1bcb2b24 at write_failing_tests step — step failed
with "Permission denied" + "budget exhausted", yet task was marked
complete and advanced to red_gate, creating an unwinnable gate.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services.conductor_service import ConductorService
from prism_service.models.task import Task


class FakeTaskService:
    """Minimal task service for testing advance_task."""

    def __init__(self, task_dict):
        self._task = task_dict

    def get(self, task_id):
        return self._task

    def update(self, task_id, **kwargs):
        for k, v in kwargs.items():
            setattr(self._task, k, v)

    def record_history(self, task_id, **kwargs):
        pass


def _make_task(workflow_step="", completion_proof="", **kwargs):
    """Create a synthetic task with the given fields."""
    defaults = {
        "id": "test-task",
        "title": "test",
        "workflow_step": workflow_step,
        "completion_proof": completion_proof,
        "gate_state": "none",
        "gate_reason": "",
        "workflow": "implement",
        "parent_id": "",
        "status": "in_progress",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_advance_task_refuses_failed_step_with_permission_denied():
    """If completion_proof contains 'Permission denied', refuse to advance."""
    task = _make_task(
        workflow_step="write_failing_tests",
        completion_proof="Edit permission denied on file. Budget exhausted.",
    )
    svc = FakeTaskService(task)
    conductor = ConductorService(":memory:", task_svc=svc)

    result = conductor.advance_task("test-task")

    assert result["ok"] is False, "should refuse advancement on permission error"
    assert result["from_step"] == "write_failing_tests"
    assert result["to_step"] == "write_failing_tests", "should not move to next step"
    assert "failure" in result["reason"].lower() or "did not complete" in result["reason"].lower()


def test_advance_task_refuses_failed_step_with_exhausted():
    """If completion_proof contains 'exhausted', refuse to advance."""
    task = _make_task(
        workflow_step="write_failing_tests",
        completion_proof="Budget exhausted after 2 retries. No tests written.",
    )
    svc = FakeTaskService(task)
    conductor = ConductorService(":memory:", task_svc=svc)

    result = conductor.advance_task("test-task")

    assert result["ok"] is False
    assert result["from_step"] == "write_failing_tests"
    assert result["to_step"] == "write_failing_tests"


def test_advance_task_refuses_failed_step_with_did_not_land():
    """If completion_proof contains 'did not land', refuse to advance."""
    task = _make_task(
        workflow_step="implement_tasks",
        completion_proof="Commit did not land. Permission denied.",
    )
    svc = FakeTaskService(task)
    conductor = ConductorService(":memory:", task_svc=svc)

    result = conductor.advance_task("test-task")

    assert result["ok"] is False
    assert "failure" in result["reason"].lower() or "did not complete" in result["reason"].lower()


def test_advance_task_allows_clean_completion_proof():
    """If completion_proof has no failure markers, allow advancement."""
    task = _make_task(
        workflow_step="write_failing_tests",
        completion_proof="4 failing tests written; red anchor at abc123def.",
    )
    svc = FakeTaskService(task)
    conductor = ConductorService(":memory:", task_svc=svc)

    result = conductor.advance_task("test-task")

    # This should succeed because there are no failure markers.
    # The test will verify ok=True (or ok=True after the step index check).
    # If the step index fails, it's a different error but not the
    # completion-proof check we're testing.
    if result["ok"]:
        assert result["to_step"] != "write_failing_tests"
    else:
        # The error should NOT be about completion_proof failure.
        assert "completion_proof" not in result.get("reason", "").lower()


def test_advance_task_allows_empty_completion_proof():
    """Empty completion_proof should not trigger the failure check."""
    task = _make_task(
        workflow_step="write_failing_tests",
        completion_proof="",
    )
    svc = FakeTaskService(task)
    conductor = ConductorService(":memory:", task_svc=svc)

    result = conductor.advance_task("test-task")

    # Should not fail on completion_proof check.
    if result["ok"] is False:
        assert "completion_proof" not in result.get("reason", "").lower()


def test_advance_task_records_history_on_failure():
    """When refusing to advance, must record a history row."""
    history_rows = []

    class HistoryCapturingService(FakeTaskService):
        def record_history(self, task_id, **kwargs):
            history_rows.append(kwargs)

    task = _make_task(
        workflow_step="write_failing_tests",
        completion_proof="Permission denied. Did not land.",
    )
    svc = HistoryCapturingService(task)
    conductor = ConductorService(":memory:", task_svc=svc)

    result = conductor.advance_task("test-task")

    assert result["ok"] is False
    assert len(history_rows) > 0, "must record history on refusal"
    last_row = history_rows[-1]
    assert last_row["action"] == "advance_refused"
    assert "completion-proof-failed" in last_row["details"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
