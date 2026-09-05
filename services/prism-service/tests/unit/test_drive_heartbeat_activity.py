"""Heartbeat-attributed 'driving' activity state (task e3b7ebf6).

A step that runs longer than the 120s conductor-transition window and the
90s session-quiet window used to render stalled/adrift with nothing wrong
-- the owner's "stalled must mean the owner has something to do" complaint.
This pins the THIRD input: a task-attributed liveness heartbeat, stored by
prism_service.services.drive_heartbeat (mirrors agent_runs_data.py) and
read by the REAL ConductorService.activity_for (services/prism-service/
prism_service/services/conductor_service.py) -- never a hand-built dict.

Covers:
  AC-5 -- a bare liveness ping missing a required field is refused BY NAME
          (BEAT_REFUSAL), never silently dropped; two beats with an
          UNCHANGED work_units counter do not advance last_progress_at, so
          a wedged/looping process resending the same counter cannot pass
          for driving.
  AC-6 -- heartbeat_age_s is scoped per task_id: a beat recorded for task B
          must not rescue task A's activity_for result.
  AC-7 -- constructs a real ConductorService + TaskService and drives the
          real activity_for -- never a hand-built activity dict.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _cond(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    return cond, task_svc, scores_db


def _old_transition(task_svc, task_id, seconds_ago):
    """Insert a stale advance_task history row so task_motion_s reads >120s
    (mirrors tests/test_activity_state.py's own helper)."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    task_svc._db.execute(
        "INSERT INTO task_history (task_id, actor, action, details, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, "", "advance_task", "to=write_failing_tests", ts),
    )
    task_svc._db.commit()


def _age_heartbeat(scores_db, task_id, seconds_ago):
    """Rewrite a recorded heartbeat's last_progress_at into the past, so a
    staleness/monotonic test doesn't need to sleep out the real window."""
    import sqlite3
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    conn = sqlite3.connect(scores_db)
    conn.execute(
        "UPDATE drive_heartbeats SET last_progress_at = ? WHERE task_id = ?",
        (ts, task_id),
    )
    conn.commit()
    conn.close()


def _driving_task(cond, task_svc):
    """A task mid a long step: BOTH the 120s motion window and the 90s
    session-quiet window have already elapsed -- the exact regression
    (owner 2026-07-30/08-06): a real 240s+ step reads stalled/adrift with
    nothing wrong."""
    t = task_svc.create(title="long step underway")
    task_svc.update(t.id, status="in_progress", workflow_step="write_failing_tests")
    _old_transition(task_svc, t.id, seconds_ago=600)
    return t


def test_fresh_heartbeat_reads_driving_past_both_stale_windows(tmp_path):
    """AC-1/AC-7: a fresh, monotonically-progressing heartbeat resolves the
    REAL activity_for to 'driving' even though motion (>120s) and session
    quiet (>90s, i.e. not adrift-eligible) are both stale."""
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = _driving_task(cond, task_svc)

    beat = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": t.id, "step": "write_failing_tests",
        "elapsed_s": 241, "last_tool": "Bash", "work_units": 1,
    })
    assert beat.get("ok") is True, beat

    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": 300})
    assert act["state"] == "driving", act


def test_fresh_heartbeat_detail_rides_the_activity_dict(tmp_path):
    """A running step shows WHAT it is doing (task 9b6800a6): while the beat
    is fresh, activity_for threads the driver's own progress evidence
    (step/last_tool/elapsed_s/age_s) through as activity["heartbeat"], so the
    board can render "driving · pytest" instead of a bare state word."""
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = _driving_task(cond, task_svc)

    beat = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": t.id, "step": "write_failing_tests",
        "elapsed_s": 241, "last_tool": "pytest", "work_units": 3,
    })
    assert beat.get("ok") is True, beat

    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": 300})
    hb = act.get("heartbeat")
    assert hb is not None, act
    assert hb["last_tool"] == "pytest", hb
    assert hb["step"] == "write_failing_tests", hb
    assert hb["elapsed_s"] == 241, hb
    assert isinstance(hb["age_s"], float), hb


def test_stale_heartbeat_detail_is_dropped_from_activity(tmp_path):
    """The passthrough is fresh-only: a beat past HEARTBEAT_WINDOW_S says
    nothing about NOW, so activity["heartbeat"] must be None (and the state
    must not read 'driving') -- a dead drive must not keep showing its last
    tool as if it were current.

    RE-SCOPED by task 1c6d59e9: this still pins a real invariant of
    conductor_service.activity_for -- ``heartbeat`` stays fresh-only and the
    ``driving`` boundary does not move. The stale detail is no longer thrown
    away, though: api/conductor.py::_with_drive_seat carries it one floor up
    on the SERVED payload under activity["beat"], marked ``stale``, so a
    driver that stopped reads differently from one that never ran. Assert the
    served contract in tests/unit/test_active_node_names_its_seat.py, not
    here."""
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = _driving_task(cond, task_svc)

    beat = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": t.id, "step": "write_failing_tests",
        "elapsed_s": 241, "last_tool": "pytest", "work_units": 3,
    })
    assert beat.get("ok") is True, beat
    _age_heartbeat(scores_db, t.id,
                   seconds_ago=drive_heartbeat.HEARTBEAT_WINDOW_S + 60)

    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": 300})
    assert act["state"] != "driving", act
    assert act.get("heartbeat") is None, act


def test_bare_liveness_ping_refused_by_name(tmp_path):
    """AC-5 (refusal half): a ping missing elapsed_s/last_tool/work_units is
    refused BY NAME, not silently dropped -- and never rescues activity_for."""
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = _driving_task(cond, task_svc)

    bare = drive_heartbeat.record_heartbeat(scores_db, {"task_id": t.id})
    assert bare.get("ok") is False
    assert bare.get("refused") == drive_heartbeat.BEAT_REFUSAL, bare

    assert drive_heartbeat.heartbeat_age_s(scores_db, t.id) is None

    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": 300})
    assert act["state"] != "driving", act


def test_unchanged_work_units_does_not_advance_progress_and_stays_stalled(tmp_path):
    """AC-5 (monotonic half): a SECOND beat carrying the SAME work_units as
    the first (a wedged/looping process resending its counter, stop_if #4)
    must not advance last_progress_at -- so once the recorded progress ages
    past HEARTBEAT_WINDOW_S, the state falls back to stalled, not driving."""
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = _driving_task(cond, task_svc)

    drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": t.id, "step": "write_failing_tests",
        "elapsed_s": 241, "last_tool": "Bash", "work_units": 1,
    })
    # Progress goes stale beyond the window.
    _age_heartbeat(scores_db, t.id, seconds_ago=drive_heartbeat.HEARTBEAT_WINDOW_S + 30)

    # A SECOND beat with the SAME work_units must NOT bump last_progress_at.
    drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": t.id, "step": "write_failing_tests",
        "elapsed_s": 300, "last_tool": "Bash", "work_units": 1,
    })

    age = drive_heartbeat.heartbeat_age_s(scores_db, t.id)
    assert age is not None and age >= drive_heartbeat.HEARTBEAT_WINDOW_S, age

    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": 300})
    assert act["state"] == "stalled", act


def test_heartbeat_is_scoped_per_task_not_leaked_across_tasks(tmp_path):
    """AC-6: a fresh beat recorded for task B must not rescue task A's
    activity_for -- heartbeat_age_s (and the driving branch that reads it)
    is keyed on task_id, never global."""
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    task_a = _driving_task(cond, task_svc)
    task_b = _driving_task(cond, task_svc)

    beat = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_b.id, "step": "write_failing_tests",
        "elapsed_s": 241, "last_tool": "Bash", "work_units": 1,
    })
    assert beat.get("ok") is True, beat

    assert drive_heartbeat.heartbeat_age_s(scores_db, task_a.id) is None

    act_a = cond.activity_for(task_svc.get(task_a.id), {"session_quiet_s": 300})
    assert act_a["state"] != "driving", act_a

    act_b = cond.activity_for(task_svc.get(task_b.id), {"session_quiet_s": 300})
    assert act_b["state"] == "driving", act_b
