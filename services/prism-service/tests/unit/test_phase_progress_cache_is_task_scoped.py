"""A task's phase_progress cache must not be evicted by an UNRELATED task.

MEASURED, 2026-09-13: `GET /api/conductor/state` cost 400-505ms and 130-144KB
on EVERY call, on the hot path for the Dashboard, the Sidebar and the
Conductor page alike, while the same endpoint served in ~5-7ms warm. The
endpoint's own N+1s were already fixed earlier that day; what remained was a
cache-INVALIDATION granularity bug.

`_phase_progress_cache_key` keyed each task's memoized progress on
`_db_change_stamp()` -- `PRAGMA data_version`, a WHOLE-FILE write counter that
moves on any write anywhere in tasks.db. With live in_progress tasks writing
heartbeats and history rows continuously, that counter moved faster than the
route's own 2.5s TTL, so a single active task evicted the cached progress of
ALL managed tasks (59 of them live) on nearly every request. Each miss re-ran
`_phase_progress_uncached`, which re-reads and re-parses live Claude transcript
JSONL -- 81,819 `json.loads` calls across 502 files on a cold process.

data_version was chosen as a deliberate SUPERSET: phase_progress's
children_done/children_total depends on CHILD rows, which this task's own
`updated_at` cannot see. The fix keeps that coverage while dropping the
file-wide blast radius -- a stamp over this task's OWN children
(`idx_tasks_parent` makes it an indexed aggregate), instead of every write in
the database.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture()
def svcs(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = "cachescope-" + uuid.uuid4().hex[:6]
    return task_svc, cond


def test_an_unrelated_task_write_does_not_evict_this_task_cache(svcs):
    """The whole point: one busy task must not invalidate every other task."""
    task_svc, cond = svcs
    mine = task_svc.create(title="the task under test", workflow="implement")
    other = task_svc.create(title="a completely unrelated task",
                            workflow="implement")

    before = cond._phase_progress_cache_key(mine.id, {})
    task_svc.update(other.id, priority=71)          # write elsewhere in the DB
    after = cond._phase_progress_cache_key(mine.id, {})

    assert before == after, (
        "an unrelated task's write changed this task's cache key, so every "
        "managed task's phase_progress is recomputed whenever ANY task is "
        f"written: {before!r} -> {after!r}")


def test_this_task_own_write_still_evicts_its_cache(svcs):
    """The cache must still notice the task itself moving."""
    task_svc, cond = svcs
    mine = task_svc.create(title="the task under test", workflow="implement")

    before = cond._phase_progress_cache_key(mine.id, {})
    task_svc.update(mine.id, priority=72)
    after = cond._phase_progress_cache_key(mine.id, {})

    assert before != after, "this task's own write must invalidate its cache"


def test_a_child_write_still_evicts_the_parent_cache(svcs):
    """Coverage that data_version was chosen FOR must survive the narrowing.

    phase_progress reports children_done/children_total, so a child moving
    genuinely changes the parent's answer even though the parent row is
    untouched.
    """
    task_svc, cond = svcs
    parent = task_svc.create(title="an epic", workflow="implement")
    child = task_svc.create(title="a child slice", workflow="implement",
                            parent_id=parent.id)

    before = cond._phase_progress_cache_key(parent.id, {})
    task_svc.update(child.id, status="done")
    after = cond._phase_progress_cache_key(parent.id, {})

    assert before != after, (
        "a child's write must still invalidate the parent's cache -- "
        "children_done/children_total depends on it")
