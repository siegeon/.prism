"""An epic-of-epics' real work stays invisible more than one level down.

Owner 2026-08-28, live, watching task 95474ec7 (an epic) read "paused ·
waiting 28:41" while a great-grandchild (95474ec7 -> 3a3f90da -> 0e2c82f3 ->
9b0f7c4b) was genuinely being driven by the daemon's task_runner seconds
earlier: "something is wrong you have been idle on that task now for quite
a long time, and it looks locked or frozen".

test_epic_activity_sees_its_driver.py already fixed the ONE-level case (a
direct child transitioning, or the epic's own linked session). Neither
signal reaches a great-grandchild: activity_for's `active` check only reads
each direct child's OWN `_task_motion_s`, never recurses into that child's
own children. An epic three levels above the real work reports "paused" or
"stalled" forever, indistinguishable from an actually-stuck task.
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


def _chain(task_svc):
    """epic -> child -> grandchild -> great_grandchild, all in_progress,
    mirroring the live 95474ec7 -> 3a3f90da -> 0e2c82f3 -> 9b0f7c4b shape.
    None of epic/child/grandchild has crossed a step boundary recently --
    only the great_grandchild (the real work) has."""
    epic = task_svc.create(title="an epic three levels above the real work")
    task_svc.update(epic.id, status="in_progress", workflow_step="green_gate")
    child = task_svc.create(title="child epic-rollup", parent_id=epic.id)
    task_svc.update(child.id, status="in_progress", workflow_step="green_gate")
    grandchild = task_svc.create(title="grandchild epic-rollup", parent_id=child.id)
    task_svc.update(grandchild.id, status="in_progress", workflow_step="green_gate")
    great_grandchild = task_svc.create(title="the actual leaf work",
                                       parent_id=grandchild.id)
    task_svc.update(great_grandchild.id, status="in_progress",
                    workflow_step="draft_story")
    return epic, child, grandchild, great_grandchild


def _pin_motion(cond, monkeypatch, motion_by_id):
    monkeypatch.setattr(
        cond, "_task_motion_s",
        lambda t, _m=motion_by_id: _m.get(getattr(t, "id", ""), None))


def test_a_great_grandchilds_fresh_motion_reads_as_working_at_the_top(
    tmp_path, monkeypatch,
):
    """THE REPORTED SYMPTOM. Only the great-grandchild transitioned recently
    (2 minutes ago, like the live 9b0f7c4b draft_story move); the epic three
    levels up must not report 'paused'/'stalled' while that is true."""
    task_svc, cond = _services(tmp_path)
    epic, child, grandchild, great_grandchild = _chain(task_svc)
    _pin_motion(cond, monkeypatch, {great_grandchild.id: 30.0})

    state = cond.activity_for(task_svc.get(epic.id), {})["state"]
    assert state not in ("paused", "stalled"), (
        f"epic three levels above active work reported {state!r} -- the "
        f"exact 'locked or frozen' misread the owner reported live")


def test_the_middle_epics_also_read_as_working_not_just_the_top(
    tmp_path, monkeypatch,
):
    """Every ancestor in the chain, not just the root, must see the
    great-grandchild's motion -- a partial fix that only patches the
    top-level call site would leave 3a3f90da and 0e2c82f3 themselves still
    misreporting even if 95474ec7 were special-cased."""
    task_svc, cond = _services(tmp_path)
    epic, child, grandchild, great_grandchild = _chain(task_svc)
    _pin_motion(cond, monkeypatch, {great_grandchild.id: 30.0})

    child_state = cond.activity_for(task_svc.get(child.id), {})["state"]
    grandchild_state = cond.activity_for(task_svc.get(grandchild.id), {})["state"]
    assert child_state not in ("paused", "stalled"), f"got {child_state}"
    assert grandchild_state not in ("paused", "stalled"), f"got {grandchild_state}"


def test_a_genuinely_quiet_deep_epic_still_reads_paused(tmp_path, monkeypatch):
    """The fix must not manufacture activity. Nothing anywhere in the
    subtree has moved recently and there is no live session: 'paused'
    (there IS a done sibling below, matching the live shape) must survive."""
    task_svc, cond = _services(tmp_path)
    epic, child, grandchild, great_grandchild = _chain(task_svc)
    done_sibling = task_svc.create(title="a finished slice", parent_id=epic.id)
    task_svc.update(done_sibling.id, status="done")
    _pin_motion(cond, monkeypatch, {})   # nothing has transitioned anywhere

    state = cond.activity_for(task_svc.get(epic.id), {"session_quiet_s": 4000.0})["state"]
    assert state == "paused", f"got {state}"
