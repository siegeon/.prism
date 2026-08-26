"""Task d67bca9f: `dependencies` (and `description`) were settable at
task_create but silently DROPPED by both update entry points --
`TaskUpdate` (REST PATCH /api/tasks/{id}) had no `dependencies` field, and
MCP `task_update`'s schema/dispatch had neither `dependencies` nor
`description`. TaskService.update already accepts arbitrary fields; only
the two entry-point contracts were blocking it. Pins:

  - MCP task_update(id, dependencies=[x]) persists and echoes [x]
  - PATCH /api/tasks/{id} {"dependencies":[x]} persists and echoes
  - a non-existent dependency id is refused (422 / MCP error)
  - a self-dependency is refused
  - description is editable over MCP
  - an unknown field on MCP task_update is refused, not silently dropped
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext so handle_tool + the API router resolve the
    SAME tmp-backed task_svc (mirrors test_task_channel_provenance)."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-task-deps"
    pc._contexts.clear()


def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


# ── MCP task_update ───────────────────────────────────────────────────

def test_mcp_task_update_persists_and_echoes_dependencies(project):
    from prism_service.project_context import get_project

    a = json.loads(_call("task_create", {"title": "a"}, project))
    b = json.loads(_call("task_create", {"title": "b"}, project))

    out = json.loads(_call(
        "task_update", {"id": a["id"], "dependencies": [b["id"]]}, project))
    assert out.get("dependencies") == [b["id"]], out

    stored = get_project(project).task_svc.get(a["id"])
    assert stored.dependencies == [b["id"]]


def test_mcp_task_update_rejects_nonexistent_dependency(project):
    a = json.loads(_call("task_create", {"title": "a"}, project))
    out = json.loads(_call(
        "task_update", {"id": a["id"], "dependencies": ["nope-not-real"]},
        project))
    assert out.get("error") == "dependencies_validation_failed", out
    assert "nope-not-real" in out.get("detail", "")


def test_mcp_task_update_refuses_self_dependency(project):
    a = json.loads(_call("task_create", {"title": "a"}, project))
    out = json.loads(_call(
        "task_update", {"id": a["id"], "dependencies": [a["id"]]}, project))
    assert out.get("error") == "dependencies_validation_failed", out
    assert a["id"] in out.get("detail", "")


def test_mcp_task_update_can_edit_description(project):
    from prism_service.project_context import get_project

    a = json.loads(_call("task_create", {"title": "a", "description": "old"}, project))
    out = json.loads(_call(
        "task_update", {"id": a["id"], "description": "new"}, project))
    assert out.get("description") == "new", out
    assert get_project(project).task_svc.get(a["id"]).description == "new"


def test_mcp_task_update_rejects_unknown_fields(project):
    a = json.loads(_call("task_create", {"title": "a"}, project))
    out = json.loads(_call(
        "task_update", {"id": a["id"], "bogus_field": "x"}, project))
    assert out.get("error") == "unknown_fields", out
    assert "bogus_field" in out.get("fields", [])


# ── REST PATCH /api/tasks/{id} ───────────────────────────────────────────

def test_patch_api_tasks_persists_and_echoes_dependencies(project):
    client = _api_client()
    a = client.post("/api/tasks", params={"project": project},
                     json={"title": "a"}).json()["task"]
    b = client.post("/api/tasks", params={"project": project},
                     json={"title": "b"}).json()["task"]

    resp = client.patch(f"/api/tasks/{a['id']}", params={"project": project},
                         json={"dependencies": [b["id"]]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["dependencies"] == [b["id"]]

    got = client.get(f"/api/tasks/{a['id']}", params={"project": project})
    assert got.json()["task"]["dependencies"] == [b["id"]]


def test_patch_api_tasks_rejects_nonexistent_dependency(project):
    client = _api_client()
    a = client.post("/api/tasks", params={"project": project},
                     json={"title": "a"}).json()["task"]
    resp = client.patch(f"/api/tasks/{a['id']}", params={"project": project},
                         json={"dependencies": ["nope-not-real"]})
    assert resp.status_code == 422, resp.text
    assert "nope-not-real" in resp.text


def test_patch_api_tasks_refuses_self_dependency(project):
    client = _api_client()
    a = client.post("/api/tasks", params={"project": project},
                     json={"title": "a"}).json()["task"]
    resp = client.patch(f"/api/tasks/{a['id']}", params={"project": project},
                         json={"dependencies": [a["id"]]})
    assert resp.status_code == 422, resp.text
    assert a["id"] in resp.text
