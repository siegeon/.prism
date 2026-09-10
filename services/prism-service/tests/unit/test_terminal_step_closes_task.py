"""Terminal step closure: tasks close when they reach a done step with no outstanding gates.

When a task advances to a step of type="done" and no gate is outstanding
(gate_state not "pending" and not "failed"), the task must close:
- status set to "done"
- completed_at set to a non-empty UTC timestamp

This is workflow-agnostic: the closure condition checks the step type and
gate state, never the workflow name. Triage reaches "done" only after "decide"
passes; implement reaches "done" only after green_gate passes. The step
sequence already encodes the oracle requirement.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from prism_service.api.conductor_flow import _close_if_terminal


class FakeTask:
    def __init__(self, **kw):
        self.id = "task-123"
        self.workflow = "implement"
        self.workflow_step = "done"
        self.gate_state = None
        self.__dict__.update(kw)


# Mock workflow steps for implement and triage workflows
MOCK_IMPLEMENT_STEPS = [
    {"id": "review_previous_notes", "type": "agent"},
    {"id": "draft_story", "type": "agent"},
    {"id": "story_gate", "type": "gate"},
    {"id": "write_failing_tests", "type": "agent"},
    {"id": "red_gate", "type": "gate"},
    {"id": "implement_tasks", "type": "agent"},
    {"id": "verify_green_state", "type": "agent"},
    {"id": "green_gate", "type": "gate"},
    {"id": "done", "type": "done"},
]

MOCK_TRIAGE_STEPS = [
    {"id": "review_previous_notes", "type": "agent"},
    {"id": "classify", "type": "agent"},
    {"id": "decide", "type": "gate"},
    {"id": "done", "type": "done"},
]


def _mock_steps_for(workflow: str):
    """Return mock steps for a given workflow."""
    if workflow == "triage":
        return MOCK_TRIAGE_STEPS
    return MOCK_IMPLEMENT_STEPS


@patch("prism_service.models.workflow.steps_for", side_effect=_mock_steps_for)
def test_a_task_on_a_done_step_with_no_outstanding_gates_closes(_mock_steps):
    """A task on type=done with no gate pending/failed gets status=done + completed_at."""
    svc = MagicMock()
    task = FakeTask(workflow="implement", workflow_step="done", gate_state=None)
    svc._task_svc.get.return_value = task

    _close_if_terminal(svc, "task-123")

    # Verify update was called with status=done and completed_at set
    svc._task_svc.update.assert_called_once()
    call_kwargs = svc._task_svc.update.call_args[1]
    assert call_kwargs.get("status") == "done"
    assert call_kwargs.get("completed_at") is not None
    # Verify completed_at is a valid ISO timestamp
    datetime.fromisoformat(call_kwargs.get("completed_at"))


@patch("prism_service.models.workflow.steps_for", side_effect=_mock_steps_for)
def test_a_task_on_a_done_step_with_pending_gate_is_not_closed(_mock_steps):
    """A task on type=done with gate_state=pending must NOT close."""
    svc = MagicMock()
    task = FakeTask(workflow="implement", workflow_step="done", gate_state="pending")
    svc._task_svc.get.return_value = task

    _close_if_terminal(svc, "task-123")

    # Verify update was NOT called
    svc._task_svc.update.assert_not_called()


@patch("prism_service.models.workflow.steps_for", side_effect=_mock_steps_for)
def test_a_task_on_a_done_step_with_failed_gate_is_not_closed(_mock_steps):
    """A task on type=done with gate_state=failed must NOT close."""
    svc = MagicMock()
    task = FakeTask(workflow="implement", workflow_step="done", gate_state="failed")
    svc._task_svc.get.return_value = task

    _close_if_terminal(svc, "task-123")

    # Verify update was NOT called
    svc._task_svc.update.assert_not_called()


@patch("prism_service.models.workflow.steps_for", side_effect=_mock_steps_for)
def test_a_task_on_a_non_terminal_step_is_not_closed(_mock_steps):
    """A task on a non-terminal step (e.g., implement_tasks) must NOT close."""
    svc = MagicMock()
    task = FakeTask(workflow="implement", workflow_step="implement_tasks", gate_state=None)
    svc._task_svc.get.return_value = task

    _close_if_terminal(svc, "task-123")

    # Verify update was NOT called
    svc._task_svc.update.assert_not_called()


@patch("prism_service.models.workflow.steps_for", side_effect=_mock_steps_for)
def test_closure_works_for_triage_workflow(_mock_steps):
    """Closure logic is workflow-agnostic and works for triage workflow."""
    svc = MagicMock()
    task = FakeTask(workflow="triage", workflow_step="done", gate_state=None)
    svc._task_svc.get.return_value = task

    _close_if_terminal(svc, "task-123")

    # Verify update was called with status=done and completed_at set
    svc._task_svc.update.assert_called_once()
    call_kwargs = svc._task_svc.update.call_args[1]
    assert call_kwargs.get("status") == "done"
    assert call_kwargs.get("completed_at") is not None


@patch("prism_service.models.workflow.steps_for", side_effect=_mock_steps_for)
def test_closure_works_for_implement_workflow(_mock_steps):
    """Closure logic works for implement workflow."""
    svc = MagicMock()
    task = FakeTask(workflow="implement", workflow_step="done", gate_state=None)
    svc._task_svc.get.return_value = task

    _close_if_terminal(svc, "task-123")

    # Verify update was called with status=done and completed_at set
    svc._task_svc.update.assert_called_once()
    call_kwargs = svc._task_svc.update.call_args[1]
    assert call_kwargs.get("status") == "done"
    assert call_kwargs.get("completed_at") is not None


def test_closure_handles_task_not_found_gracefully():
    """If the task is not found, closure returns without error."""
    svc = MagicMock()
    svc._task_svc.get.return_value = None

    # Should not raise
    _close_if_terminal(svc, "task-123")
    svc._task_svc.update.assert_not_called()


@patch("prism_service.models.workflow.steps_for", side_effect=Exception("broken"))
def test_closure_handles_workflow_resolution_error_gracefully(_mock_steps):
    """If workflow resolution fails, closure returns without error."""
    svc = MagicMock()
    task = FakeTask(workflow="implement", workflow_step="done", gate_state=None)
    svc._task_svc.get.return_value = task

    # Should not raise
    _close_if_terminal(svc, "task-123")
    svc._task_svc.update.assert_not_called()


@patch("prism_service.models.workflow.steps_for", side_effect=_mock_steps_for)
def test_closure_handles_update_error_gracefully(_mock_steps):
    """If the update call fails, closure handles it without re-raising."""
    svc = MagicMock()
    task = FakeTask(workflow="implement", workflow_step="done", gate_state=None)
    svc._task_svc.get.return_value = task
    svc._task_svc.update.side_effect = Exception("update failed")

    # Should not raise
    _close_if_terminal(svc, "task-123")


@patch("prism_service.models.workflow.steps_for", side_effect=_mock_steps_for)
def test_closure_does_not_set_full_outcome_complete(_mock_steps):
    """Closure sets status and completed_at only; never full_outcome_complete."""
    svc = MagicMock()
    task = FakeTask(workflow="implement", workflow_step="done", gate_state=None)
    svc._task_svc.get.return_value = task

    _close_if_terminal(svc, "task-123")

    svc._task_svc.update.assert_called_once()
    call_kwargs = svc._task_svc.update.call_args[1]
    assert "full_outcome_complete" not in call_kwargs
    assert call_kwargs.get("status") == "done"
    assert "completed_at" in call_kwargs


@patch("prism_service.models.workflow.steps_for", side_effect=_mock_steps_for)
def test_closure_on_done_step_with_passed_gate_closes(_mock_steps):
    """A task on type=done with gate_state=passed (not pending/failed) closes."""
    svc = MagicMock()
    task = FakeTask(workflow="implement", workflow_step="done", gate_state="passed")
    svc._task_svc.get.return_value = task

    _close_if_terminal(svc, "task-123")

    # Should close because gate_state is not "pending" and not "failed"
    svc._task_svc.update.assert_called_once()
    call_kwargs = svc._task_svc.update.call_args[1]
    assert call_kwargs.get("status") == "done"


@patch("prism_service.models.workflow.steps_for", side_effect=_mock_steps_for)
def test_completed_at_timestamp_is_valid_iso_format(_mock_steps):
    """The completed_at timestamp is valid ISO 8601 UTC format."""
    svc = MagicMock()
    task = FakeTask(workflow="implement", workflow_step="done", gate_state=None)
    svc._task_svc.get.return_value = task

    _close_if_terminal(svc, "task-123")

    svc._task_svc.update.assert_called_once()
    call_kwargs = svc._task_svc.update.call_args[1]
    timestamp_str = call_kwargs.get("completed_at")

    # Should be parseable as ISO format
    parsed = datetime.fromisoformat(timestamp_str)
    # Should have timezone info (UTC)
    assert parsed.tzinfo is not None
    # Should be close to now
    now = datetime.now(timezone.utc)
    assert abs((now - parsed).total_seconds()) < 5
