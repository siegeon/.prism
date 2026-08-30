"""A driven task must READ as driven, whatever its status word says.

Owner 2026-08-30, looking at the board while four agents worked:
"I still dont see you playing a task."

CONFIRMED CAUSE. `activity_for` short-circuited on the raw status BEFORE it
read any liveness evidence:

    if status == "done":        state = "done"
    elif status == "blocked":   state = "blocked"
    elif status == "pending":   state = "pending"
    elif status == "in_progress": ...only here was the heartbeat consulted

So a task with a driver actively beating on it rendered as idle backlog
(pending) or as an alarm the owner had to act on (blocked). Measured live on
the board that day: of 38 managed tiles, 24 read pending and 10 read blocked,
and NOT ONE read working or driving -- while real drives were in flight.

status is the LAST thing a driver updates; the heartbeat is the FIRST. A
fresh beat is therefore better evidence of now than the status word, and it
wins -- except for `done`, where terminal work must never return to the
board as live no matter who beats on it.
"""

from __future__ import annotations

import uuid


def _svc(project: str):
    from prism_service.project_context import get_project
    return get_project(project).conductor_svc


def _beat(svc, task_id: str, units: int) -> None:
    from prism_service.services import drive_heartbeat
    drive_heartbeat.record_heartbeat(svc._scores_db, {
        "task_id": task_id, "step": "implement_tasks", "elapsed_s": 12,
        "last_tool": "Edit", "work_units": units, "driver": "a-real-driver",
    })


def _state(svc, task):
    return svc.activity_for(task, svc.phase_progress(task.id))


def test_a_pending_task_with_a_live_beat_reads_driving():
    svc = _svc("beatrank-" + uuid.uuid4().hex[:8])
    t = svc._task_svc.create(title="a driver is on it, status not yet flipped")
    svc._task_svc.update(t.id, status="pending", workflow_step="implement_tasks")
    _beat(svc, t.id, 1)
    got = _state(svc, svc._task_svc.get(t.id))
    assert got["state"] == "driving", (
        "a fresh beat is proof a driver is on this task NOW; rendering it as "
        f"idle backlog is what hid every drive from the owner -- got {got!r}")
    assert got["heartbeat"] is not None, (
        f"the tile must carry the driver's own progress evidence -- got {got!r}")


def test_a_blocked_task_with_a_live_beat_reads_driving_not_an_alarm():
    svc = _svc("beatrank-" + uuid.uuid4().hex[:8])
    t = svc._task_svc.create(title="blocked word, live driver")
    svc._task_svc.update(t.id, status="blocked", workflow_step="implement_tasks")
    _beat(svc, t.id, 1)
    got = _state(svc, svc._task_svc.get(t.id))
    assert got["state"] == "driving", (
        "'blocked' is an ALARM word the owner reads as 'I must intervene'. "
        "While a driver is beating on the task nobody needs to intervene -- "
        f"got {got!r}")


def test_a_done_task_never_reads_driving_however_it_is_beaten_on():
    """THE GUARD. Terminal work must never return to the board as live."""
    svc = _svc("beatrank-" + uuid.uuid4().hex[:8])
    t = svc._task_svc.create(title="already shipped")
    svc._task_svc.update(t.id, status="done", workflow_step="green_gate")
    _beat(svc, t.id, 1)
    got = _state(svc, svc._task_svc.get(t.id))
    assert got["state"] == "done", (
        f"a done task stays done whoever beats on it -- got {got!r}")


def test_a_pending_task_with_no_beat_still_reads_pending():
    """THE OTHER GUARD. The change is additive: absent liveness evidence,
    idle backlog still reads as idle backlog."""
    svc = _svc("beatrank-" + uuid.uuid4().hex[:8])
    t = svc._task_svc.create(title="nobody is on it")
    svc._task_svc.update(t.id, status="pending", workflow_step="implement_tasks")
    got = _state(svc, svc._task_svc.get(t.id))
    assert got["state"] == "pending", (
        f"no beat means no driver; the tile must not claim one -- got {got!r}")
