"""RED scaffold — conductor tile must tell the truth about FINISHED tasks
(task 77470287). Three bugs found reviewing completed task 7bdb5701's telemetry:

  #1 phase_progress() returns pct 0.0 on a DONE task because a CANCELLED
     ephemeral-fixture child is counted in children_done/children_total
     (0/1). Fix: cancelled children don't count; a done task is 100%.
  #5 _in_step_s() returns now-last_advance forever, so a finished task's
     dwell grows without bound (5.5h+). Fix: freeze at completed_at for
     terminal tasks.
  #3 link_session() stamps a truncated 8-char session id alongside the real
     UUID (phantom zero-stat row). Fix: skip a session id that is a strict
     prefix of an already-linked, longer id for the same task.

All FAIL before the fix on feat/conductor-tile-correctness.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PID = "done_tile_pid"


def _conductor(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    proj_dir = tmp_path / "projects" / _PID
    proj_dir.mkdir(parents=True, exist_ok=True)
    scores_db = str(proj_dir / "scores.db")
    task_svc = TaskService(str(proj_dir / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ----------------------------------------------------------------------
# #1a — a cancelled child (ephemeral fixture) must NOT count toward progress.
# ----------------------------------------------------------------------
def test_cancelled_child_excluded_from_progress(tmp_path):
    task_svc, cond = _conductor(tmp_path)
    parent = task_svc.create(title="parent in flight")
    task_svc.update(parent.id, workflow_step="implement_tasks")
    fixture = task_svc.create(title="FIXTURE", parent_id=parent.id)
    task_svc.update(fixture.id, status="cancelled")

    pp = cond.phase_progress(parent.id)
    # The only child is cancelled -> it must not appear in the denominator.
    assert pp["children_total"] == 0, pp
    # ...so progress falls back to the time basis, NOT a 0/1 children ratio.
    assert pp["basis"] != "children", pp


# ----------------------------------------------------------------------
# A soft-deleted child must not count either — same exclusion as cancelled.
# _child_task_ids (services/conductor_service.py:4549) already excludes both
# ("cancelled", "deleted"); phase_progress's own children loop only excluded
# "cancelled", so an epic's tile (children_total) could disagree with its own
# epic_rollup_verdict (which also only names "cancelled" — deleted rows are
# the gap both must close together).
# ----------------------------------------------------------------------
def test_deleted_child_excluded_from_progress(tmp_path):
    task_svc, cond = _conductor(tmp_path)
    parent = task_svc.create(title="parent in flight")
    task_svc.update(parent.id, workflow_step="implement_tasks")
    fixture = task_svc.create(title="removed", parent_id=parent.id)
    task_svc.update(fixture.id, status="deleted")

    pp = cond.phase_progress(parent.id)
    assert pp["children_total"] == 0, pp
    assert pp["basis"] != "children", pp


def test_children_total_excludes_cancelled_and_deleted_together(tmp_path):
    """Real shape from epic 0784729f: 15 child rows (7 done, 2 cancelled, 6
    live) read 7/13 on epic_rollup_verdict but 7/14 or 7/18 on the tile
    whenever a cancelled/deleted row slips into the denominator. Both must
    agree: 7/13."""
    task_svc, cond = _conductor(tmp_path)
    parent = task_svc.create(title="epic")
    task_svc.update(parent.id, workflow_step="implement_tasks")
    for i in range(7):
        c = task_svc.create(title=f"done-{i}", parent_id=parent.id)
        task_svc.update(c.id, status="done")
    for i in range(2):
        c = task_svc.create(title=f"cancelled-{i}", parent_id=parent.id)
        task_svc.update(c.id, status="cancelled")
    for i in range(3):
        c = task_svc.create(title=f"deleted-{i}", parent_id=parent.id)
        task_svc.update(c.id, status="deleted")
    for i in range(6):
        task_svc.create(title=f"live-{i}", parent_id=parent.id)

    pp = cond.phase_progress(parent.id)
    assert pp["children_total"] == 13, pp
    assert pp["children_done"] == 7, pp


# ----------------------------------------------------------------------
# #1b — a DONE task reports 100%, never 0%, regardless of child bookkeeping.
# ----------------------------------------------------------------------
def test_done_task_reports_full_progress(tmp_path):
    task_svc, cond = _conductor(tmp_path)
    parent = task_svc.create(title="finished task")
    task_svc.update(parent.id, workflow_step="green_gate")
    # a cancelled fixture child, exactly like the real 7bdb5701 case
    fixture = task_svc.create(title="FIXTURE", parent_id=parent.id)
    task_svc.update(fixture.id, status="cancelled")
    task_svc.update(parent.id, status="done")

    pp = cond.phase_progress(parent.id)
    assert pp["pct"] == pytest.approx(1.0), pp
    assert pp["basis"] == "done", pp


# ----------------------------------------------------------------------
# #5 — in_step_s FREEZES for a terminal task instead of tracking wall-clock.
# ----------------------------------------------------------------------
def test_in_step_s_frozen_for_done_task(tmp_path):
    task_svc, cond = _conductor(tmp_path)
    t = task_svc.create(title="done long ago")
    # Advance INTO the final step a long time ago, complete shortly after.
    long_ago = datetime.now(timezone.utc) - timedelta(days=2)
    task_svc._db.execute(
        "INSERT INTO task_history (task_id, actor, action, details, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (t.id, "conductor", "advance_task", "from=x; to=green_gate",
         _iso(long_ago)),
    )
    task_svc._db.commit()
    completed = long_ago + timedelta(seconds=120)
    task_svc.update(t.id, workflow_step="green_gate", status="done")
    # stamp a deterministic completed_at
    task_svc._db.execute(
        "UPDATE tasks SET completed_at=? WHERE id=?", (_iso(completed), t.id))
    task_svc._db.commit()

    in_step = cond._in_step_s(t.id)
    # Frozen at completed_at - advance == 120s (not ~2 days of wall-clock).
    assert in_step == pytest.approx(120.0, abs=2.0), in_step


# ----------------------------------------------------------------------
# #3 — link_session skips a truncated-prefix phantom session id.
# ----------------------------------------------------------------------
def test_link_session_skips_truncated_prefix_phantom(tmp_path):
    import sqlite3

    task_svc, _ = _conductor(tmp_path)
    t = task_svc.create(title="task with sessions")
    real = "6d42fcd4-f7e2-4853-ab00-8da16b64773a"
    task_svc.link_session(t.id, real)
    # The phantom truncated id (first 8 chars) must be refused.
    task_svc.link_session(t.id, "6d42fcd4")

    # Read the link table directly — sessions_for_task needs session_outcomes
    # which isn't materialized in this unit harness.
    conn = sqlite3.connect(task_svc._scores_db)
    try:
        sids = {r[0] for r in conn.execute(
            "SELECT session_id FROM task_sessions WHERE task_id=?", (t.id,)
        ).fetchall()}
    finally:
        conn.close()
    assert real in sids, sids
    assert "6d42fcd4" not in sids, sids
