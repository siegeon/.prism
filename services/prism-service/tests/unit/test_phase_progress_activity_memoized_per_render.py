"""phase_progress()/activity_for() are memoized per task_id (route-timing
pass, owner brief 2026-09-13): managed_tasks() computes both for every
managed task, and GET /api/work/graph's child loop calls them AGAIN for a
child managed_tasks() already covered in the same render -- profiled live,
that double invocation is the whole cost of the route's `edges`+`tasks_query`
phases (managed_tasks() alone was 526ms of an 849ms in-process render).

Memoization is ONLY enabled when the caller passes `heartbeat_cache` (a
task_id -> beat dict, as `drive_heartbeat.latest_many` returns) -- see the
docstrings on both methods for the caught bug: without a heartbeat_cache the
uncached path falls back to a direct `drive_heartbeat.latest()` disk read,
but the cache key has no cheap way to represent that read's ANSWER (only
whether a cache dict happened to be passed), so a caller not sharing one
(e.g. task_service.py's own update()-triggered call, which runs BEFORE a
driver's first beat) would otherwise cache "no heartbeat" until some
unrelated task-row write busted it, hiding a real heartbeat recorded in the
meantime from every OTHER caller reading the same task_id afterward
(reproduced live via tests/unit/test_a_fresh_beat_outranks_the_status_word.py
going red under the naive always-cache design). heartbeat_cache=None (every
pre-existing caller) is therefore always a full recompute, byte-identical to
pre-memoization behavior; only managed_tasks() and GET /api/work/graph pass
a real one.

These pin: (1) with heartbeat_cache given, a second call for the SAME
task_id with nothing changed is a literal cache hit (same dict object
back); (2) a task row write busts it; (3) a fresh heartbeat busts it even
with no task write; (4) activity_for additionally busts on a different
`session_quiet_s` in the passed phase_progress dict; (5) heartbeat_cache=None
NEVER caches, so no caller that omits it can ever see a stale answer."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    return task_svc, cond


def test_second_phase_progress_call_with_heartbeat_cache_is_a_hit(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="steady task")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")

    beats: dict = {}
    first = cond.phase_progress(t.id, heartbeat_cache=beats)
    second = cond.phase_progress(t.id, heartbeat_cache=beats)
    assert second is first, (
        "a repeat phase_progress() call with the same heartbeat_cache and "
        "nothing changed must return the memoized object, not recompute")


def test_phase_progress_with_no_heartbeat_cache_never_caches(tmp_path):
    """THE CAUGHT BUG. A caller that never shares a heartbeat_cache (every
    pre-existing call site: task_service.py's own update()-triggered call,
    resume_actuator.py, api/tasks.py, and the direct-call tests) must see a
    live heartbeat the MOMENT it lands, never a cached pre-heartbeat answer
    from an earlier call with the same task row unchanged."""
    from prism_service.services import drive_heartbeat

    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="beating task")
    task_svc.update(t.id, status="pending", workflow_step="implement_tasks")

    # A call BEFORE any heartbeat exists, exactly like task_service.py's own
    # internal phase_progress/activity_for call during update() -- no task
    # row write happens between this and the real check below.
    task = task_svc.get(t.id)
    cond.activity_for(task, cond.phase_progress(t.id))

    drive_heartbeat.record_heartbeat(cond._scores_db, {
        "task_id": t.id, "step": "implement_tasks", "elapsed_s": 12,
        "last_tool": "Edit", "work_units": 1, "driver": "a-real-driver",
    })
    task = task_svc.get(t.id)
    got = cond.activity_for(task, cond.phase_progress(t.id))
    assert got["state"] == "driving", (
        f"a fresh heartbeat with no heartbeat_cache anywhere must still be "
        f"seen live -- got {got!r} (a cached pre-heartbeat answer would "
        f"read 'pending')")


def test_a_task_write_busts_the_phase_progress_cache(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="advancing task")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")

    beats: dict = {}
    first = cond.phase_progress(t.id, heartbeat_cache=beats)
    task_svc.update(t.id, workflow_step="write_failing_tests")
    second = cond.phase_progress(t.id, heartbeat_cache=beats)
    assert second is not first, (
        "a workflow_step write must bust the cache -- got the same object "
        "back after a real task-row change")
    assert second["basis"] in ("time", "children", "fanout", "done")


def test_a_fresh_heartbeat_busts_the_phase_progress_cache_alone(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="beating task")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")

    beats: dict = {}
    first = cond.phase_progress(t.id, heartbeat_cache=beats)
    beats = {t.id: {"recorded_at": "2026-09-13T00:00:00+00:00", "age_s": 1.0}}
    second = cond.phase_progress(t.id, heartbeat_cache=beats)
    assert second is not first, (
        "a fresh heartbeat with no task-row write must still bust the "
        "cache -- that's the live-transcript-growth signal")
    third = cond.phase_progress(t.id, heartbeat_cache=beats)
    assert third is second, "same heartbeat snapshot twice must be a hit"


def test_second_activity_for_call_with_heartbeat_cache_is_a_hit(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="steady task")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")
    task = task_svc.get(t.id)
    beats: dict = {}
    pp = cond.phase_progress(t.id, heartbeat_cache=beats)

    first = cond.activity_for(task, pp, heartbeat_cache=beats)
    second = cond.activity_for(task, pp, heartbeat_cache=beats)
    assert second is first


def test_activity_for_with_no_heartbeat_cache_never_caches(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="epic")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")
    task = task_svc.get(t.id)

    first = cond.activity_for(task, {"session_quiet_s": 5.0})
    second = cond.activity_for(task, {"session_quiet_s": 4000.0})
    assert second is not first, (
        "with no heartbeat_cache, every call must fully recompute -- two "
        "different inputs must never collapse onto the same object")


def test_activity_for_busts_on_different_session_quiet_s(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="epic")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")
    task = task_svc.get(t.id)
    beats: dict = {}

    first = cond.activity_for(task, {"session_quiet_s": 5.0}, heartbeat_cache=beats)
    second = cond.activity_for(task, {"session_quiet_s": 4000.0}, heartbeat_cache=beats)
    assert second is not first, (
        "two calls with different session_quiet_s in the passed "
        "phase_progress dict must never share a cache slot")
