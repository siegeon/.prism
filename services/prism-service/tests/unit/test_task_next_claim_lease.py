"""Claim lease on task_next (task 41af13c0-6716-49a4-8c5a-7310bf8a0db8).

STRESS-LOOP FINDING: task_next / conductor claiming had no lease — two
concurrent drivers could grab the SAME task (observed live: 9f61d484 was
double-driven). next_task now atomically stamps claimed_by + claimed_at and
skips tasks claimed within a freshness window by a DIFFERENT session, so a
second next_task from another session returns a DIFFERENT task. Unclaimed and
expired-claim tasks are still returned (backward compatible).

RED before the lease lands: next_task() takes no session_id, so the
session-scoped calls raise TypeError and the distinct-task guarantee is absent.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"))


# AC-1 — two distinct sessions never claim the same task.
def test_distinct_sessions_get_distinct_tasks(tmp_path):
    svc = _svc(tmp_path)
    svc.create(title="A", priority=10)
    svc.create(title="B", priority=5)
    first = svc.next_task(session_id="sess-A")["task"]
    second = svc.next_task(session_id="sess-B")["task"]
    assert first.id != second.id, (
        "a task freshly claimed by sess-A must not be handed to sess-B"
    )


# AC-2 — the claim is stamped onto the returned task (claimed_by/claimed_at).
def test_next_task_stamps_claim(tmp_path):
    svc = _svc(tmp_path)
    svc.create(title="A", priority=10)
    got = svc.next_task(session_id="sess-A")["task"]
    row = svc.get(got.id)
    assert row.claimed_by == "sess-A" and row.claimed_at, (
        "next_task must stamp claimed_by + claimed_at on the returned task"
    )


# AC-3 — the same session re-claiming gets its own task back (idempotent).
def test_same_session_reclaims_its_own(tmp_path):
    svc = _svc(tmp_path)
    svc.create(title="A", priority=10)
    svc.create(title="B", priority=5)
    first = svc.next_task(session_id="sess-A")["task"]
    again = svc.next_task(session_id="sess-A")["task"]
    assert first.id == again.id, "a session keeps its own fresh claim"


# AC-4 — an expired claim is reclaimable by another session.
def test_expired_claim_is_reclaimable(tmp_path):
    svc = _svc(tmp_path)
    svc.create(title="A", priority=10)
    svc.create(title="B", priority=5)
    first = svc.next_task(session_id="sess-A")["task"]
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    svc._db.execute(
        "UPDATE tasks SET claimed_at=? WHERE id=?", (stale, first.id))
    svc._db.commit()
    reclaimed = svc.next_task(session_id="sess-C", lease_window_s=900)["task"]
    assert reclaimed.id == first.id, (
        "a claim older than the freshness window is up for grabs again"
    )


# AC-5 — backward compatible: no session_id keeps the legacy behavior.
def test_no_session_is_backward_compatible(tmp_path):
    svc = _svc(tmp_path)
    a = svc.create(title="A", priority=10)
    svc.create(title="B", priority=5)
    assert svc.next_task()["task"].id == a.id, (
        "a session-less next_task still returns the highest-priority task"
    )
