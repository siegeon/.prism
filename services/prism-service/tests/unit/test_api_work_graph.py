"""GET /api/work/graph -- the /live boot snapshot (gamify walking skeleton).

Seeds a real TaskService with one root task (conductor-managed: status
in_progress + a workflow_step) and one child task, drives the REAL
work_graph() handler over a REAL ConductorService (enable_engine=False,
same pattern as test_drive_heartbeat_activity.py's _cond() helper), and
asserts the response contains a task node for the root, a subtask node for
the child, and a parent_of edge between them -- the minimum shape the
/live SPA page needs to paint anything on boot.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _seeded_ctx(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic — walking skeleton")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    child = task_svc.create(title="Child slice — backend publishers", parent_id=root.id)
    task_svc.update(child.id, status="in_progress")

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc
    return ctx, root.id, child.id


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    ctx, root_id, child_id = _seeded_ctx(tmp_path)
    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    return TestClient(app), root_id, child_id


def test_route_registered_on_api_router():
    from fastapi import FastAPI
    from prism_service.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    paths = set(app.openapi()["paths"].keys())
    assert "/api/work/graph" in paths, (
        f"GET /api/work/graph not mounted on api_router: {sorted(paths)}")
    assert "/api/work/sim-tokens" in paths, (
        f"POST /api/work/sim-tokens not mounted on api_router: {sorted(paths)}")


def test_graph_returns_task_and_subtask_with_parent_of_edge(tmp_path, monkeypatch):
    client, root_id, child_id = _client(tmp_path, monkeypatch)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    body = resp.json()
    assert "nodes" in body and "edges" in body and "generated_at" in body, (
        f"missing top-level keys; got {sorted(body.keys())}")

    nodes_by_id = {n["id"]: n for n in body["nodes"]}
    assert root_id in nodes_by_id, f"root task node missing; got {body['nodes']!r}"
    assert child_id in nodes_by_id, f"child subtask node missing; got {body['nodes']!r}"

    root_node = nodes_by_id[root_id]
    assert root_node["kind"] == "task", f"got {root_node!r}"
    assert root_node["status"] == "in_progress", f"got {root_node!r}"
    assert root_node["workflow_step"] == "implement_tasks", f"got {root_node!r}"
    assert root_node["href"] == f"/tasks/{root_id}", f"got {root_node!r}"

    child_node = nodes_by_id[child_id]
    assert child_node["kind"] == "subtask", f"got {child_node!r}"
    assert child_node["href"] == f"/tasks/{child_id}", f"got {child_node!r}"

    edges = body["edges"]
    assert any(
        e.get("source") == root_id and e.get("target") == child_id
        and e.get("kind") == "parent_of"
        for e in edges
    ), f"expected a parent_of edge {root_id}->{child_id}; got {edges!r}"


def test_graph_dedupes_a_child_that_is_also_independently_managed(tmp_path, monkeypatch):
    # A subtask that ALSO carries its own workflow_step/gate_state is
    # returned by managed_tasks() a SECOND time as its own top-level
    # entry (real board behavior: "an ENGAGED child... MUST surface").
    # Caught live via the gamify sim: a child sim advanced through
    # /api/conductor/advance rendered as two separate circles with the
    # same id in the /live graph. The node LIST must carry each id once.
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    child = task_svc.create(title="Independently-managed child", parent_id=root.id)
    # The child is ALSO independently in_progress with its own step —
    # this is what makes managed_tasks() surface it a second time.
    task_svc.update(child.id, status="in_progress", workflow_step="write_failing_tests")

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    ids = [n["id"] for n in nodes]
    assert ids.count(child.id) == 1, (
        f"child {child.id} must appear exactly once even though it is "
        f"both a subtask AND independently managed; got node ids {ids!r}")
    edges = resp.json()["edges"]
    assert any(
        e.get("source") == root.id and e.get("target") == child.id
        and e.get("kind") == "parent_of" for e in edges
    ), f"parent_of edge must still be recorded; got {edges!r}"


def test_graph_omits_unmanaged_projects_task(tmp_path, monkeypatch):
    # A task that never entered the conductor (no workflow_step, status
    # pending) must not appear -- managed_tasks() is the source of truth.
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    idle = task_svc.create(title="Untouched backlog item")

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    ids = {n["id"] for n in resp.json()["nodes"]}
    assert idle.id not in ids, (
        f"an idle, never-managed task must not appear on the live graph; got {ids!r}")
