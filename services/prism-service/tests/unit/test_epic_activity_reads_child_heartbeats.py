"""An epic reads alive while a slice drives (task 8eba8871).

Observed live on epic fcf6b70b, 2026-08-14: child 31c345b7 was actively
driving (activity.state=driving, a fresh drive_heartbeats row on ITS row)
while the parent epic rendered adrift/stalled -- alarm-adjacent wording for
a healthy, actively-driven epic.

ConductorService.activity_for (services/prism-service/prism_service/
services/conductor_service.py, kids branch) reads a child's TRANSITIONS
(`_task_motion_s(k) <= 120`) for `working`, and the EPIC'S OWN heartbeat
(`drive_heartbeat.latest(scores_db, task.id)`) for `driving` -- but never a
child's heartbeat. A child lane mid-step crosses no step boundary for most
of its life; its heartbeat is the only proof it is alive, and the epic
branch throws it away.

Pins (task oracle):
  AC-1 -- an in_progress epic whose in_progress child has a heartbeat
          fresher than HEARTBEAT_WINDOW_S, with NO recent transitions on
          either row, reads as active work (working/driving), never
          adrift/paused/stalled.  RED today.
  AC-2 -- with the child beat stale (or absent) and nothing else fresh the
          epic still decays to paused/stalled exactly as today -- the
          honest-decay negative case.  GREEN today and must stay green.
Guards (task likely_misfire): the child beat maps to `driving` (liveness
evidence), not `working` (a step boundary); a beat is task-scoped -- an
unrelated task's beat, or a DONE child's beat, rescues nothing.

Drives the REAL ConductorService + TaskService against a real sqlite
scores.db -- never a hand-built activity dict.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def _services(tmp_path):
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    return task_svc, cond, scores_db


def _epic(task_svc, with_done_sibling=False):
    """An in_progress epic with one in_progress child that has crossed no
    step boundary. Motion is pinned explicitly (see _pin_motion) because a
    fresh row falls back to updated_at and would read active for reasons
    unrelated to the branch under test."""
    epic = task_svc.create(title="an epic the owner watches")
    task_svc.update(epic.id, status="in_progress",
                    workflow_step="implement_tasks")
    child = task_svc.create(title="a slice mid-step", parent_id=epic.id)
    task_svc.update(child.id, status="in_progress",
                    workflow_step="write_failing_tests")
    done = None
    if with_done_sibling:
        done = task_svc.create(title="a finished slice", parent_id=epic.id)
        task_svc.update(done.id, status="done")
    return epic, child, done


def _pin_motion(cond, monkeypatch, motion_by_id):
    """Pin _task_motion_s per task id; missing => None (no motion at all)."""
    monkeypatch.setattr(
        cond, "_task_motion_s",
        lambda t, _m=motion_by_id: _m.get(getattr(t, "id", ""), None))


def _beat(scores_db, task_id, work_units=1):
    from prism_service.services import drive_heartbeat

    r = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": "write_failing_tests",
        "elapsed_s": 241, "last_tool": "pytest", "work_units": work_units,
    })
    assert r.get("ok") is True, r
    return r


def _age_heartbeat(scores_db, task_id, seconds_ago):
    """Backdate a recorded beat's last_progress_at so the stale case never
    has to sleep out HEARTBEAT_WINDOW_S (own helper; mirrors the one in
    test_drive_heartbeat_activity.py, which is not importable)."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    conn = sqlite3.connect(scores_db)
    conn.execute(
        "UPDATE drive_heartbeats SET last_progress_at = ? WHERE task_id = ?",
        (ts, task_id),
    )
    conn.commit()
    conn.close()


def _state(cond, task_svc, epic):
    # {} => session_quiet_s absent: the adrift rung cannot fire, so the only
    # liveness left on the table is the heartbeat channel.
    return cond.activity_for(task_svc.get(epic.id), {})["state"]


# --- AC-1: a fresh child heartbeat makes the epic read alive (RED today) ---

def test_fresh_child_heartbeat_means_the_epic_is_actively_worked(tmp_path, monkeypatch):
    """THE REPORTED SYMPTOM (epic fcf6b70b / child 31c345b7, 2026-08-14).
    No transition on the epic or the child; the child's drive heartbeat is
    fresh. The epic must name active work, never adrift/paused/stalled."""
    task_svc, cond, scores_db = _services(tmp_path)
    epic, child, _ = _epic(task_svc)
    _pin_motion(cond, monkeypatch, {})
    _beat(scores_db, child.id)

    state = _state(cond, task_svc, epic)
    assert state in ("working", "driving"), (
        f"epic read {state!r} while its child lane had a fresh heartbeat - "
        "the alarm-adjacent reading the owner saw on fcf6b70b")
    assert state not in ("adrift", "paused", "stalled")


def test_child_heartbeat_is_liveness_so_the_epic_reads_driving_not_working(tmp_path, monkeypatch):
    """likely_misfire (2): a beat is liveness evidence, not a step boundary.
    The epic's OWN beat maps to `driving` one rung above; a child's beat
    must land on the same word, preserving the working/driving distinction."""
    task_svc, cond, scores_db = _services(tmp_path)
    epic, child, _ = _epic(task_svc)
    _pin_motion(cond, monkeypatch, {})
    _beat(scores_db, child.id)

    assert _state(cond, task_svc, epic) == "driving"


def test_fresh_child_heartbeat_beats_the_done_sibling_paused_reading(tmp_path, monkeypatch):
    """With a finished sibling the fallback is `paused`; a driving child must
    still win over it -- `paused` means between bursts, and a burst is on."""
    task_svc, cond, scores_db = _services(tmp_path)
    epic, child, _ = _epic(task_svc, with_done_sibling=True)
    _pin_motion(cond, monkeypatch, {})
    _beat(scores_db, child.id)

    state = _state(cond, task_svc, epic)
    assert state in ("working", "driving"), f"got {state}"


# --- AC-2: honest decay is preserved (GREEN today, must stay green) ---

def test_stale_child_heartbeat_still_decays_to_stalled(tmp_path, monkeypatch):
    """likely_misfire (1): a beat older than HEARTBEAT_WINDOW_S is not
    liveness. No done sibling, nothing fresh anywhere => stalled, as today."""
    from prism_service.services import drive_heartbeat

    task_svc, cond, scores_db = _services(tmp_path)
    epic, child, _ = _epic(task_svc)
    _pin_motion(cond, monkeypatch, {})
    _beat(scores_db, child.id)
    _age_heartbeat(scores_db, child.id,
                   seconds_ago=drive_heartbeat.HEARTBEAT_WINDOW_S + 60)

    assert _state(cond, task_svc, epic) == "stalled"


def test_stale_child_heartbeat_with_done_sibling_still_reads_paused(tmp_path, monkeypatch):
    """Same decay, `paused` variant: progress was made, the burst is over."""
    from prism_service.services import drive_heartbeat

    task_svc, cond, scores_db = _services(tmp_path)
    epic, child, _ = _epic(task_svc, with_done_sibling=True)
    _pin_motion(cond, monkeypatch, {})
    _beat(scores_db, child.id)
    _age_heartbeat(scores_db, child.id,
                   seconds_ago=drive_heartbeat.HEARTBEAT_WINDOW_S + 60)

    assert _state(cond, task_svc, epic) == "paused"


def test_no_heartbeat_anywhere_still_reads_stalled(tmp_path, monkeypatch):
    """Absent variant of AC-2: no beat on any row, no motion => stalled."""
    task_svc, cond, scores_db = _services(tmp_path)
    epic, child, _ = _epic(task_svc)
    _pin_motion(cond, monkeypatch, {})

    assert _state(cond, task_svc, epic) == "stalled"


# --- scoping guards: liveness is attributed, never widened ---

def test_an_unrelated_tasks_heartbeat_does_not_rescue_the_epic(tmp_path, monkeypatch):
    """A fresh beat on a task that is NOT a child of this epic is someone
    else's liveness. Counting it would keep a dead epic green forever."""
    task_svc, cond, scores_db = _services(tmp_path)
    epic, child, _ = _epic(task_svc)
    stranger = task_svc.create(title="an unrelated lane")
    task_svc.update(stranger.id, status="in_progress")
    _pin_motion(cond, monkeypatch, {})
    _beat(scores_db, stranger.id)

    assert _state(cond, task_svc, epic) == "stalled"


def test_a_done_childs_fresh_heartbeat_does_not_count(tmp_path, monkeypatch):
    """Only an in_progress child can drive. A beat left on a child that has
    since gone `done` is history, not liveness => paused (progress, no burst)."""
    task_svc, cond, scores_db = _services(tmp_path)
    epic, child, done = _epic(task_svc, with_done_sibling=True)
    _pin_motion(cond, monkeypatch, {})
    _beat(scores_db, done.id)

    assert _state(cond, task_svc, epic) == "paused"
