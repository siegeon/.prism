"""Honest per-task activity classifier — ConductorService.activity_for.

The /conductor tile used to read the raw task STATUS ("in_progress" + a live
burn graph) even when nothing was driving the task — a lie. activity_for
computes the REAL work state from two signals: task_motion_s (recency of the
last conductor transition on THIS task, from task_history) and session_quiet_s
(recency of the linked session's live transcript, carried on phase_progress).

state table (see conductor_service.activity_for):
  done / blocked / pending          <- straight from status
  in_progress:
    awaiting_gate  at a *_gate with gate_state pending/failed  (a WAIT)
    working        a conductor transition moved THIS task <=120s ago
    adrift         session alive (<=90s) but task idle >120s
    stalled        both stale — nothing is driving it
"""

from datetime import datetime, timezone, timedelta


def _cond(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    return cond, task_svc


def _old_transition(task_svc, task_id, seconds_ago):
    """Insert an advance_task history row dated `seconds_ago` in the past so
    task_motion_s reads stale (real transitions write `now`; we can't)."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    task_svc._db.execute(
        "INSERT INTO task_history (task_id, actor, action, details, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, "", "advance_task", "to=implement_tasks", ts),
    )
    task_svc._db.commit()


def test_working_recent_transition(tmp_path):
    cond, task_svc = _cond(tmp_path)
    t = task_svc.create(title="being driven")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")
    # A conductor transition on THIS task just now → motion ~0s.
    task_svc.record_history(t.id, action="advance_task", details="to=implement_tasks")
    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": None})
    assert act["state"] == "working"
    assert act["task_motion_s"] is not None and act["task_motion_s"] <= 120


def test_awaiting_gate(tmp_path):
    cond, task_svc = _cond(tmp_path)
    t = task_svc.create(title="at a gate")
    task_svc.update(t.id, status="in_progress",
                    workflow_step="green_gate", gate_state="pending")
    # Recent motion must NOT mask the gate wait.
    task_svc.record_history(t.id, action="advance_task", details="to=green_gate")
    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": 5})
    assert act["state"] == "awaiting_gate"


def test_adrift_session_alive_task_idle(tmp_path):
    cond, task_svc = _cond(tmp_path)
    t = task_svc.create(title="session busy elsewhere")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")
    _old_transition(task_svc, t.id, seconds_ago=600)   # task idle > 120s
    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": 30})  # alive
    assert act["state"] == "adrift"
    assert act["task_motion_s"] > 120


def test_stalled_both_stale(tmp_path):
    cond, task_svc = _cond(tmp_path)
    t = task_svc.create(title="nothing driving it")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")
    _old_transition(task_svc, t.id, seconds_ago=600)   # task idle > 120s
    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": None})  # quiet
    assert act["state"] == "stalled"


def test_stalled_when_session_also_quiet(tmp_path):
    cond, task_svc = _cond(tmp_path)
    t = task_svc.create(title="session quiet too")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")
    _old_transition(task_svc, t.id, seconds_ago=600)
    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": 300})  # >90
    assert act["state"] == "stalled"


def test_done_and_blocked(tmp_path):
    cond, task_svc = _cond(tmp_path)
    d = task_svc.create(title="shipped")
    task_svc.update(d.id, status="done")
    assert cond.activity_for(task_svc.get(d.id), {})["state"] == "done"

    b = task_svc.create(title="blocked on dep")
    task_svc.update(b.id, status="blocked")
    assert cond.activity_for(task_svc.get(b.id), {})["state"] == "blocked"


def test_pending_stays_pending(tmp_path):
    cond, task_svc = _cond(tmp_path)
    p = task_svc.create(title="backlog")
    assert cond.activity_for(task_svc.get(p.id), {})["state"] == "pending"
