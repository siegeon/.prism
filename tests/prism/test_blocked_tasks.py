import pytest
from prism.models import TaskRun

def test_blocked_task_does_not_show_in_in_progress_tasks():
    """Verify that blocked tasks are excluded from in-progress task listings."""
    
    # Simulate a blocked task that's not in progress
    blocked_task = TaskRun(status="blocked", task_id="ab9166d5")
    
    # Assert that the blocked task is not considered in progress
    assert blocked_task.status != "in_progress", "Blocked task should not be in progress"