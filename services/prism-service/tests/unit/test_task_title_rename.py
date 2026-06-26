"""Task title rename — a task's TITLE must be editable (customer bug 11040b39).

Pins, RED-first, that `title` threads through the ONE task_update path across
all three layers, plus a BLANK-TITLE GUARD (None/empty/whitespace is ignored,
never blanks an existing title):
  * TaskService.update(id, title=...) persists; "   " leaves the title alone.
  * The MCP dispatcher SEAM (handle_tool -> task_update) advertises `title`
    in its schema AND plumbs it end-to-end (durable, not echo).
  * The API SEAM (PATCH /api/tasks/{id}) accepts `title` and GET echoes it.

These FAIL before the guard + schema/arg/model wiring exist.
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


def _services(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext so handle_tool + the API router resolve the
    SAME tmp-backed task_svc (mirrors test_full_outcome_complete)."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-title-rename"
    pc._contexts.clear()


# ---- service layer: round-trip + blank guard --------------------------

def test_update_persists_new_title(tmp_path):
    task_svc = _services(tmp_path)
    t = task_svc.create(title="Original")
    task_svc.update(t.id, title="Renamed")
    assert task_svc.get(t.id).title == "Renamed"


def test_update_blank_title_is_ignored(tmp_path):
    task_svc = _services(tmp_path)
    t = task_svc.create(title="Keep Me")
    for blank in ("", "   ", "\t\n", None):
        task_svc.update(t.id, title=blank)
        assert task_svc.get(t.id).title == "Keep Me", blank


# ---- MCP dispatcher SEAM (handle_tool, not the service method) ---------

def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def test_task_update_schema_advertises_title():
    from prism_service.mcp.tools import TOOLS
    by_name = {t.name: t for t in TOOLS}
    assert "title" in by_name["task_update"].inputSchema["properties"]


def test_mcp_task_update_plumbs_title(project):
    from prism_service.project_context import get_project

    created = json.loads(_call(
        "task_create", {"title": "via mcp original"}, project))
    tid = created["id"]
    json.loads(_call(
        "task_update", {"id": tid, "title": "via mcp renamed"}, project))
    # Survives a SEPARATE read through the project context (durable, not echo).
    assert get_project(project).task_svc.get(tid).title == "via mcp renamed"


# ---- API SEAM (real route, PATCH then GET) ----------------------------

def _api_client(project_id):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app), project_id


def test_api_patch_renames_title(project):
    client, pid = _api_client(project)
    post = client.post(
        "/api/tasks", params={"project": pid}, json={"title": "api original"})
    assert post.status_code in (200, 201), post.text
    tid = post.json()["task"]["id"]

    patched = client.patch(
        f"/api/tasks/{tid}", params={"project": pid},
        json={"title": "Renamed via API"})
    assert patched.status_code == 200, patched.text

    got = client.get(f"/api/tasks/{tid}", params={"project": pid})
    assert got.json()["task"]["title"] == "Renamed via API"
