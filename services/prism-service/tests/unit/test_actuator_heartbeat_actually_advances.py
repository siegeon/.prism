"""The resume actuator's own liveness beat must refresh on every dispatch.

dispatch_once's docstring promises "the tile moves off 'stalled' the instant
dispatch fires, whatever the outcome". It wrote work_units=1, a CONSTANT.

drive_heartbeat.record_heartbeat is deliberately MONOTONIC: a beat repeating
the previously stored work_units does not advance last_progress_at, so "a
wedged or looping process resending its own counter cannot keep resetting its
staleness clock". A hardcoded 1 therefore refreshed this seat's liveness
exactly ONCE -- on the first dispatch ever -- and never again. Every later
rescue left the task reading `stalled` 180s later, no matter how many times
the actuator had actually just picked it up.

Observed 2026-08-30: three real dispatches on task 7a72ebcb, all writing
work_units=1, with the tile still reading stalled between them.
"""

from __future__ import annotations

import uuid


def _svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / f"t-{uuid.uuid4().hex[:8]}.db"))


def test_dispatch_count_strictly_increases_with_each_dispatch(tmp_path):
    from prism_service.services import resume_actuator as ra

    svc = _svc(tmp_path)
    t = svc.create(title="a task the seat keeps rescuing")

    first = ra._dispatch_count(svc, t.id)
    svc.record_history(t.id, action=ra.DISPATCH_ACTION,
                       details="seat=x; step=implement_tasks", actor=ra.SEAT)
    second = ra._dispatch_count(svc, t.id)
    svc.record_history(t.id, action=ra.DISPATCH_ACTION,
                       details="seat=x; step=verify_green_state", actor=ra.SEAT)
    third = ra._dispatch_count(svc, t.id)

    assert first < second < third, (
        "the counter this seat feeds to work_units must strictly increase, or "
        "drive_heartbeat's monotonic guard silently drops every beat after "
        f"the first -- got {first}, {second}, {third}")


def test_a_repeated_counter_would_not_advance_the_clock(tmp_path):
    """WHY THE ABOVE MATTERS, pinned against the real heartbeat store: a beat
    reusing its previous work_units leaves last_progress_at untouched."""
    from prism_service.services import drive_heartbeat as hb

    db = str(tmp_path / "scores.db")
    tid = uuid.uuid4().hex

    row = {"task_id": tid, "step": "implement_tasks", "elapsed_s": 0,
           "last_tool": "resume_actuator_dispatch", "work_units": 1}
    first = hb.record_heartbeat(db, dict(row))
    repeat = hb.record_heartbeat(db, dict(row))
    assert repeat["last_progress_at"] == first["last_progress_at"], (
        "a repeated work_units must NOT advance the clock -- this is the "
        "guard that made the actuator's constant beat inert")

    row["work_units"] = 2
    advanced = hb.record_heartbeat(db, dict(row))
    assert advanced["last_progress_at"] != first["last_progress_at"], (
        "an increased work_units MUST advance the clock, or the fix buys "
        "nothing")


def test_the_seat_never_reports_zero_work_units(tmp_path):
    """work_units is a REQUIRED field and 0 is falsy: record_heartbeat
    refuses a row whose required field is empty, so the seat must never
    emit 0 on a task with no dispatch history yet."""
    from prism_service.services import resume_actuator as ra

    svc = _svc(tmp_path)
    t = svc.create(title="never dispatched before")
    assert ra._dispatch_count(svc, t.id) >= 1, (
        "a first dispatch must still emit a truthy work_units, or the beat "
        "is refused outright as a missing required field")
