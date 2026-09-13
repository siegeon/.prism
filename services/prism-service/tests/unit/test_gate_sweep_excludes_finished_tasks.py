"""gate_sweep_rows() must not keep re-fetching tasks whose STATUS is already
finished (done/cancelled/deleted/archived) forever -- measured live: 316 of
325 rows the sweep fetched were finished, 272 of those done+green_gate+passed,
and the sweep re-examines that dead weight (plus the task_changed churn it
emits) on every single pass. The predicate narrows on workflow_step only,
never status, so a task that finished long ago but still carries a gate
workflow_step keeps coming back.

Distinguish this from the terminal-STEP-closure branch in
gate_adjudicator.sweep_once (step == "done" -> _close_if_terminal): that
branch closes a task whose STEP reached "done" but whose STATUS is still
live (pending/in_progress/blocked) -- excluding by status must NOT remove
those rows, only rows whose status itself is already finished.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services.task_service import TaskService  # noqa: E402


def _svc(tmp_path):
    return TaskService(str(tmp_path / "tasks.db"),
                        scores_db=str(tmp_path / "scores.db"))


def _mk(task_svc, status, step, title):
    t = task_svc.create(title=title)
    task_svc.update(t.id, status=status, workflow_step=step)
    return t.id


def test_finished_tasks_are_excluded_from_the_gate_sweep(tmp_path):
    task_svc = _svc(tmp_path)

    finished_ids = {
        _mk(task_svc, "done", "green_gate", "finished done"),
        _mk(task_svc, "cancelled", "green_gate", "finished cancelled"),
        _mk(task_svc, "deleted", "red_gate", "finished deleted"),
        _mk(task_svc, "archived", "story_gate", "finished archived"),
    }
    live_ids = {
        _mk(task_svc, "pending", "green_gate", "live pending"),
        _mk(task_svc, "in_progress", "plan_gate", "live in_progress"),
        _mk(task_svc, "blocked", "decide", "live blocked"),
    }

    rows = task_svc.gate_sweep_rows()
    seen = {r["id"] for r in rows}

    assert seen.isdisjoint(finished_ids), \
        f"finished tasks leaked into the sweep: {seen & finished_ids}"
    assert live_ids <= seen, \
        f"live tasks dropped from the sweep: {live_ids - seen}"


def test_a_live_task_sitting_at_the_terminal_step_is_still_swept(tmp_path):
    """A task at workflow_step='done' whose STATUS has not yet been closed
    (in_progress) is exactly what gate_adjudicator's terminal-closure branch
    needs to see -- the status exclusion must not remove it."""
    task_svc = _svc(tmp_path)

    awaiting_close = _mk(task_svc, "in_progress", "done", "awaiting closure")
    already_closed = _mk(task_svc, "done", "done", "already closed")

    seen = {r["id"] for r in task_svc.gate_sweep_rows()}

    assert awaiting_close in seen
    assert already_closed not in seen
