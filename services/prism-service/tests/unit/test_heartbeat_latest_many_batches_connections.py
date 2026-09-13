"""managed_tasks()/work_graph() open ONE drive_heartbeat connection per
request, not one per task (tick-cost pass, external fixer, owner brief
2026-09-13, no PRISM ticket -- the last ~30ms of GET /api/conductor/state
and GET /api/work/graph: activity_for() called drive_heartbeat.latest(),
which opened a fresh sqlite3 connection to scores.db, once per managed
task -- 125 opens measured for a 58-task render).

activity_for() now accepts an optional heartbeat_cache (a task_id -> beat
dict from the existing drive_heartbeat.latest_many, batched once by the
caller); managed_tasks()/work_graph()/_with_drive_seat all fetch it once
up front. These tests count real drive_heartbeat._connect calls via a
wrapper, so a future re-introduction of the per-task open goes red here."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_N_TASKS = 12


def _seed(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services import drive_heartbeat

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    ids = []
    for i in range(_N_TASKS):
        t = task_svc.create(title=f"managed task {i}")
        task_svc.update(t.id, status="in_progress")
        cond.advance_task(t.id)
        drive_heartbeat.record_heartbeat(scores_db, {
            "task_id": t.id, "step": "implement_tasks", "elapsed_s": 1,
            "last_tool": "Bash", "work_units": 1,
        })
        ids.append(t.id)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = cond
    ctx.task_svc = task_svc
    return ctx, task_svc, cond, scores_db, ids


def _counted_connect(monkeypatch, module):
    calls = []
    real = module._connect

    def _spy(scores_db):
        calls.append(scores_db)
        return real(scores_db)

    monkeypatch.setattr(module, "_connect", _spy)
    return calls


def test_managed_tasks_opens_one_heartbeat_connection_not_one_per_task(
        tmp_path, monkeypatch):
    from prism_service.services import drive_heartbeat
    ctx, task_svc, cond, scores_db, ids = _seed(tmp_path)

    calls = _counted_connect(monkeypatch, drive_heartbeat)
    rows = cond.managed_tasks()

    assert len(rows) == _N_TASKS
    assert len(calls) <= 1, (
        f"expected at most one drive_heartbeat connection open for "
        f"{_N_TASKS} tasks, got {len(calls)}")
    assert all((r["activity"].get("heartbeat") or {}).get("age_s") is not None
               for r in rows), (
        "the batched heartbeat_cache must still surface the recorded "
        "beat's age via activity['heartbeat'], same as the unbatched "
        "per-task lookup")


def test_work_graph_opens_one_heartbeat_connection_not_one_per_task(
        tmp_path, monkeypatch):
    from prism_service.api import work as work_api
    from prism_service.services import drive_heartbeat
    ctx, task_svc, cond, scores_db, ids = _seed(tmp_path)
    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)

    calls = _counted_connect(monkeypatch, drive_heartbeat)
    body = work_api.work_graph(project="p")

    task_nodes = [n for n in body["nodes"] if n["id"] in ids]
    assert len(task_nodes) == _N_TASKS
    assert len(calls) <= 1, (
        f"expected at most one drive_heartbeat connection open for "
        f"{_N_TASKS} tasks, got {len(calls)}")


def test_with_drive_seat_opens_one_heartbeat_connection_not_one_per_row(
        tmp_path, monkeypatch):
    """_with_drive_seat is a SEPARATE enrichment pass over managed_tasks()'s
    already-built rows (api/conductor.py's state() route calls both), so it
    opens its own one connection independent of managed_tasks()'s own --
    the fix is one connection PER PASS, not a shared global cache across
    passes. managed_tasks() runs here BEFORE the connect counter starts, so
    only _with_drive_seat's own call is counted."""
    from prism_service.api import conductor as conductor_api
    from prism_service.services import drive_heartbeat
    ctx, task_svc, cond, scores_db, ids = _seed(tmp_path)

    rows_in = cond.managed_tasks()
    calls = _counted_connect(monkeypatch, drive_heartbeat)
    rows = conductor_api._with_drive_seat(rows_in, scores_db, task_svc)

    assert len(rows) == _N_TASKS
    assert len(calls) <= 1, (
        f"expected at most one drive_heartbeat connection open for "
        f"{_N_TASKS} rows, got {len(calls)}")
