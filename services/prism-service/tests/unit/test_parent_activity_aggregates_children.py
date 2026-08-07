"""A parent's activity aggregates the work happening in its children.

Owner 2026-08-06: "subtasks are all managed by you, not me ... but i do care
when you say the activity is done... right now it should be working on any
number of sub tasks who stats aggregate to the reporting, like token per
second should be close to 600+ since we are using 3 sub agents on 4
separate tasks".

THE DEFECT. ConductorService._task_motion_s (conductor_service.py:4517)
reads advance_task/gate_decide rows for THIS task id only, and
phase_progress's token loop reads sessions_for_task(task_id) only. So an
epic with three lanes burning underneath it has no motion of its own and
renders "paused - 0 tok/s" while real work is happening. The board tells the
owner nothing is going on precisely when the most is.

Subtasks are the driver's decomposition, so they must not appear as peers on
the board - but their WORK is the parent's work and has to roll up, or the
parent's readout is a lie in the other direction.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    return task_svc, cond


def test_a_child_transition_counts_as_the_parents_motion(tmp_path):
    """THE REPORTED SYMPTOM. The epic itself has not transitioned in hours,
    but a slice under it just advanced — so the epic is working, not paused."""
    task_svc, cond = _services(tmp_path)
    epic = task_svc.create(title="an epic the owner watches")
    child = task_svc.create(title="a slice the driver owns", parent_id=epic.id)

    # The epic itself records nothing; only the child moves.
    task_svc.record_history(child.id, action="advance_task",
                            details="from=draft_story; to=verify_plan",
                            actor="conductor")

    motion = cond._task_motion_s(task_svc.get(epic.id))
    assert motion is not None, (
        "a parent with an actively-advancing child must report motion")
    assert motion < 120, (
        f"the child advanced just now, so the parent is working; got {motion}s")


def test_a_parent_with_idle_children_still_reads_idle(tmp_path):
    """The aggregation must not manufacture motion. A parent whose children
    have done nothing has no motion to report — under-claim, never over."""
    task_svc, cond = _services(tmp_path)
    epic = task_svc.create(title="an epic with nothing happening")
    task_svc.create(title="a slice nobody started", parent_id=epic.id)

    assert cond._task_motion_s(task_svc.get(epic.id)) is None


def test_a_childs_motion_does_not_leak_to_an_unrelated_task(tmp_path):
    """Roll-up follows parentage, never 'any recent activity anywhere'."""
    task_svc, cond = _services(tmp_path)
    epic = task_svc.create(title="the busy epic")
    child = task_svc.create(title="its slice", parent_id=epic.id)
    other = task_svc.create(title="an unrelated root task")

    task_svc.record_history(child.id, action="advance_task",
                            details="from=x; to=y", actor="conductor")

    assert cond._task_motion_s(task_svc.get(epic.id)) is not None
    assert cond._task_motion_s(task_svc.get(other.id)) is None
