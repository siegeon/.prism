"""An epic whose driver is working does not read "paused".

Owner 2026-08-06, repeatedly, while real work was in flight: "it still
looks like its frozen", "it looks like you are still doing nothing".

ConductorService.activity_for has two entirely different paths. The
CHILDLESS path consults three signals: a recent transition on the task
(working), a live linked session (adrift), else stalled. The path for a
task WITH CHILDREN consults only its children:

    active = any(child in_progress and child transitioned <= 120s)
    if active: working
    elif done > 0: paused
    else: stalled

So an epic is "paused" whenever no CHILD crossed a step boundary in the
last two minutes — even when its own driver is mid-step, writing code, or
when the epic itself just transitioned. The one signal that proves a human
or agent is actually at work (the live session transcript) is never read on
this path at all.

Work that produces no step boundary is invisible, and a long step produces
none for most of its life. That is the whole reported symptom.
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


def _epic(task_svc):
    """An epic with one finished slice and one slice that is in_progress but
    has NOT crossed a step boundary recently — the shape the owner kept
    seeing. Motion is controlled explicitly below rather than relied on from
    row timestamps, because a freshly-created row falls back to updated_at
    and would read as active for reasons that have nothing to do with the
    branch under test."""
    epic = task_svc.create(title="an epic the owner watches")
    task_svc.update(epic.id, status="in_progress",
                    workflow_step="implement_tasks")
    done = task_svc.create(title="a finished slice", parent_id=epic.id)
    task_svc.update(done.id, status="done")
    idle = task_svc.create(title="a slice between bursts", parent_id=epic.id)
    task_svc.update(idle.id, status="in_progress")
    return epic, idle


def _pin_motion(cond, monkeypatch, motion_by_id):
    """Pin _task_motion_s per task id; None means 'no motion recorded'."""
    monkeypatch.setattr(
        cond, "_task_motion_s",
        lambda t, _m=motion_by_id: _m.get(getattr(t, "id", ""), None))


def test_a_live_session_means_the_epic_is_not_paused(tmp_path, monkeypatch):
    """THE REPORTED SYMPTOM. No child has transitioned recently, but the
    driver is demonstrably alive (the linked transcript moved 5s ago), so
    the epic must not tell the owner that nothing is happening."""
    task_svc, cond = _services(tmp_path)
    epic, idle = _epic(task_svc)
    _pin_motion(cond, monkeypatch, {})   # nothing has transitioned at all

    state = cond.activity_for(task_svc.get(epic.id),
                              {"session_quiet_s": 5.0})["state"]
    assert state != "paused", (
        "an epic with a live driver reported 'paused' - the frozen reading "
        "the owner kept seeing while work was genuinely in flight")


def test_a_genuinely_quiet_epic_still_reads_paused(tmp_path, monkeypatch):
    """The fix must not manufacture activity. Nothing driving it and no live
    session: 'paused' is honest and must survive."""
    task_svc, cond = _services(tmp_path)
    epic, idle = _epic(task_svc)
    _pin_motion(cond, monkeypatch, {})

    state = cond.activity_for(task_svc.get(epic.id),
                              {"session_quiet_s": 4000.0})["state"]
    assert state == "paused"


def test_the_epics_own_transition_counts_as_working(tmp_path, monkeypatch):
    """An epic driven directly - its OWN steps advancing, no child moving -
    is working. On the children path that case is invisible today."""
    task_svc, cond = _services(tmp_path)
    epic, idle = _epic(task_svc)
    _pin_motion(cond, monkeypatch, {epic.id: 10.0})

    state = cond.activity_for(task_svc.get(epic.id), {})["state"]
    assert state == "working", f"got {state}"


def test_an_actively_moving_child_still_means_working(tmp_path, monkeypatch):
    """The existing contract, unchanged: a slice crossing step boundaries
    makes the epic working."""
    task_svc, cond = _services(tmp_path)
    epic, idle = _epic(task_svc)
    _pin_motion(cond, monkeypatch, {idle.id: 8.0})

    state = cond.activity_for(task_svc.get(epic.id), {})["state"]
    assert state == "working", f"got {state}"
