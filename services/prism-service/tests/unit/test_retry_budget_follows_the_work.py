"""A spent retry budget must not park a task that has since ADVANCED
(task 338f7810, 2026-09-04).

`dispatch_once` resets the attempt count when ITS OWN report advances the
task. But any seat may advance it — task_runner drives on its own sweep —
and the actuator's leftover count then parks a task that is making
progress.

LIVE REGRESSION, from task 338f7810's own history:
  00:03:00  advance_refused  premise_grounded  (attempts charged)
  00:05:10  advance_task     review_previous_notes -> draft_story
  00:06:02  status in_progress -> blocked
            "resume-actuator: retry budget spent (3/3) — parked for a human"
The step SUCCEEDED, and 52 seconds later the seat parked the task over
attempts spent at the PREVIOUS step. Same shape as the rewind/stall-budget
defect: a counter that outlives the work it was counting.

The sweep now asks whether a conductor transition landed after the last
charged attempt, and resets instead of parking when one did. It fails
CLOSED — no timestamp, an unparsable one, or a read error leaves the park
standing — so this can only ever spare a task that demonstrably moved.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture()
def task():
    from prism_service.project_context import get_project

    project = "budget-follows-" + uuid.uuid4().hex[:8]
    ctx = get_project(project)
    t = ctx.task_svc.create(title="budget task")
    ctx.task_svc.update(t.id, status="in_progress",
                        workflow_step="review_previous_notes")
    return ctx, t.id, project


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds_ago)).isoformat()


def _advance_row(ctx, task_id: str, seconds_ago: float) -> None:
    """A real conductor transition, back-stamped `seconds_ago`."""
    import sqlite3

    ctx.task_svc.record_history(
        task_id, action="advance_task",
        details="from=review_previous_notes; to=draft_story",
        actor="conductor")
    conn = sqlite3.connect(str(ctx._data_dir / "tasks.db"))
    try:
        conn.execute(
            "UPDATE task_history SET timestamp = ? "
            "WHERE task_id = ? AND action = 'advance_task'",
            (_iso(seconds_ago), task_id))
        conn.commit()
    finally:
        conn.close()


def test_a_transition_after_the_last_attempt_reads_as_advanced(task):
    from prism_service.services import resume_actuator as ra

    ctx, task_id, project = task
    _advance_row(ctx, task_id, seconds_ago=10)     # moved 10s ago
    charged = _iso(60)                             # last charged 60s ago

    assert ra._advanced_since(project, task_id, charged) is True


def test_a_transition_before_the_last_attempt_is_not_progress(task):
    """The attempts were charged AFTER the last transition, so the work
    has genuinely not moved since — the park must stand."""
    from prism_service.services import resume_actuator as ra

    ctx, task_id, project = task
    _advance_row(ctx, task_id, seconds_ago=300)    # moved long ago
    charged = _iso(10)                             # charged since then

    assert ra._advanced_since(project, task_id, charged) is False


def test_no_recorded_attempt_time_fails_closed(task):
    """Fail CLOSED: with nothing to compare against, the park stands."""
    from prism_service.services import resume_actuator as ra

    ctx, task_id, project = task
    _advance_row(ctx, task_id, seconds_ago=10)

    assert ra._advanced_since(project, task_id, "") is False
    assert ra._advanced_since(project, task_id, "not-a-timestamp") is False


def test_a_dispatch_row_alone_is_not_progress(task):
    """A seat TRYING is not the work moving. Only a conductor transition
    may clear a budget — otherwise the actuator's own dispatch rows would
    keep resetting the count it just charged."""
    from prism_service.services import resume_actuator as ra

    ctx, task_id, project = task
    ctx.task_svc.record_history(
        task_id, action=ra.DISPATCH_ACTION,
        details="seat=prism-resume-actuator; step=review_previous_notes",
        actor=ra.SEAT)

    assert ra._advanced_since(project, task_id, _iso(300)) is False


def test_sweep_resets_instead_of_parking_when_the_task_advanced(
        task, monkeypatch):
    """End to end: a spent budget plus a later transition dispatches
    again rather than parking — the live 338f7810 shape."""
    from prism_service.services import resume_actuator as ra
    from prism_service.services import resume_attempts_data as rad

    ctx, task_id, project = task
    scores_db = ra._scores_db_for(project)
    for _ in range(ra._max_retries()):
        rad.record_attempt(scores_db, task_id)
    _advance_row(ctx, task_id, seconds_ago=0)   # moved just now

    monkeypatch.setattr(ra, "_open_retry_task_id", lambda p: task_id)
    dispatched: list[str] = []
    monkeypatch.setattr(ra, "dispatch_once",
                        lambda p, t: dispatched.append(t) or {"ok": True})

    res = ra.sweep_once_for(project)

    assert dispatched == [task_id], "an advanced task must be driven, not parked"
    assert res.get("parked") is not True
    assert rad.attempt_count(scores_db, task_id) == 0
    assert ctx.task_svc.get(task_id).status == "in_progress"


def test_sweep_still_parks_a_task_that_has_not_moved(task, monkeypatch):
    """The budget is narrowed, never removed: a genuinely stuck task with
    no transition since its attempts still parks."""
    from prism_service.services import resume_actuator as ra
    from prism_service.services import resume_attempts_data as rad

    ctx, task_id, project = task
    scores_db = ra._scores_db_for(project)
    _advance_row(ctx, task_id, seconds_ago=600)   # moved long before
    for _ in range(ra._max_retries()):
        rad.record_attempt(scores_db, task_id)

    monkeypatch.setattr(ra, "_open_retry_task_id", lambda p: task_id)
    monkeypatch.setattr(
        ra, "dispatch_once",
        lambda p, t: (_ for _ in ()).throw(
            AssertionError("must not dispatch a genuinely stuck task")))

    res = ra.sweep_once_for(project)

    assert res.get("parked") is True
    assert ctx.task_svc.get(task_id).status == "blocked"
