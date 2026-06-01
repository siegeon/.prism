"""RED scaffold — render task plans as rich HTML (task a69d30dd).

Pins the FULL user-facing vertical slice for the new Task.plan_doc /
Task.plan_diagram fields, end-to-end through the REAL seams — not just a
service method in isolation (that exact gap has shipped false-greens):

  Seam 1 (model + persistence) — TaskService.create()/update() accept and
    PERSIST plan_doc/plan_diagram, and row hydration reads them back from a
    SEPARATE service instance over the same tasks.db (proves on-disk, not
    in-memory).
  Seam 2 (additive migration) — a tasks.db created by an OLD TaskService
    that lacks the plan_* columns is migrated additively (ALTER TABLE, no
    row rewrite) so a pre-existing row survives and the new columns
    backfill to ''.
  Seam 3 (MCP dispatcher) — task_create / task_update are reachable
    THROUGH _dispatch_tool (the way the MCP server invokes them) carrying
    plan_doc/plan_diagram, and the tool inputSchemas advertise both params.
  Seam 4 (API) — GET /api/tasks/{id} returns plan_doc/plan_diagram on the
    task object, and PATCH /api/tasks/{id} (TaskUpdate) PERSISTS them
    rather than silently dropping them through the kwargs filter.

ALL FAIL today: the dataclass has no plan_doc/plan_diagram fields, the
schema + migration list lack the columns, create()/update() don't accept
them, the MCP tool schemas + handlers don't pass them, and TaskUpdate
whitelists neither.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


_DIAGRAM = "sequenceDiagram\n  User->>PRISM: open task\n  PRISM-->>User: render plan"
_DOC = "## Proposed change\n\n- Add a Plan card\n- Render Mermaid on top\n"


def _svc(tmp_path, name="tasks.db"):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / name))


# ----------------------------------------------------------------------
# Seam 1 — model field + on-disk persistence round-trip
# ----------------------------------------------------------------------

def test_plan_fields_default_empty(tmp_path):
    t = _svc(tmp_path).create(title="x")
    assert t.plan_doc == "" and t.plan_diagram == ""


def test_plan_fields_persist_across_separate_instances(tmp_path):
    """create() persists plan_doc/plan_diagram; a SEPARATE TaskService
    over the same db hydrates them back — proves on-disk, not in-memory."""
    svc = _svc(tmp_path)
    t = svc.create(title="x", plan_doc=_DOC, plan_diagram=_DIAGRAM)

    fresh = _svc(tmp_path)  # new connection, same file
    g = fresh.get(t.id)
    assert g.plan_doc == _DOC
    assert g.plan_diagram == _DIAGRAM


def test_update_persists_plan_fields(tmp_path):
    svc = _svc(tmp_path)
    t = svc.create(title="x")
    svc.update(t.id, plan_doc=_DOC, plan_diagram=_DIAGRAM)
    assert _svc(tmp_path).get(t.id).plan_diagram == _DIAGRAM
    assert _svc(tmp_path).get(t.id).plan_doc == _DOC


# ----------------------------------------------------------------------
# Seam 2 — additive migration on a PRE-EXISTING db (no row rewrite)
# ----------------------------------------------------------------------

def test_additive_migration_on_preexisting_db(tmp_path):
    """A tasks.db created WITHOUT the plan_* columns must migrate
    additively: the old row survives and the new columns backfill to ''."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '', "
        "status TEXT DEFAULT 'pending', priority INTEGER DEFAULT 0, "
        "story_file TEXT DEFAULT '', assigned_agent TEXT DEFAULT '', "
        "created_at TEXT NOT NULL, updated_at TEXT DEFAULT '', "
        "completed_at TEXT DEFAULT '', blocked_reason TEXT DEFAULT '', "
        "dependencies TEXT DEFAULT '[]', tags TEXT DEFAULT '[]')"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, created_at) VALUES (?, ?, ?)",
        ("legacy-1", "old task", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    from prism_service.services.task_service import TaskService
    svc = TaskService(str(db))  # triggers _migrate_task_columns

    cols = {r[1] for r in svc._db.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "plan_doc" in cols and "plan_diagram" in cols

    legacy = svc.get("legacy-1")
    assert legacy is not None and legacy.title == "old task"
    assert legacy.plan_doc == "" and legacy.plan_diagram == ""


# ----------------------------------------------------------------------
# Seam 3 — MCP dispatcher reachability (NOT just schema-defined)
# ----------------------------------------------------------------------

def _isolated_project(tmp_path, monkeypatch, pid="test-plan-doc"):
    from prism_service import config as cfg
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    from prism_service import project_context as pc
    pc._contexts.clear()
    return pid


def _call(name, arguments, pid):
    from prism_service.mcp.tools import handle_tool
    out = asyncio.run(handle_tool(name, arguments, project_id=pid))
    assert len(out) == 1
    return json.loads(out[0].text)


def test_task_tool_schemas_advertise_plan_params():
    from prism_service.mcp.tools import TOOLS
    by_name = {t.name: t for t in TOOLS}
    for tool in ("task_create", "task_update"):
        props = by_name[tool].inputSchema["properties"]
        assert "plan_doc" in props, f"{tool} missing plan_doc param"
        assert "plan_diagram" in props, f"{tool} missing plan_diagram param"


def test_task_create_through_dispatcher_carries_plan(tmp_path, monkeypatch):
    pid = _isolated_project(tmp_path, monkeypatch)
    created = _call("task_create", {
        "title": "render plan", "plan_doc": _DOC, "plan_diagram": _DIAGRAM,
    }, pid)
    assert created["plan_doc"] == _DOC
    assert created["plan_diagram"] == _DIAGRAM


def test_task_update_through_dispatcher_persists_plan(tmp_path, monkeypatch):
    pid = _isolated_project(tmp_path, monkeypatch)
    created = _call("task_create", {"title": "render plan"}, pid)
    tid = created["id"]
    _call("task_update", {
        "id": tid, "plan_doc": _DOC, "plan_diagram": _DIAGRAM,
    }, pid)
    # read back through the dispatcher's own project context
    from prism_service.project_context import get_project
    g = get_project(pid).task_svc.get(tid)
    assert g.plan_diagram == _DIAGRAM and g.plan_doc == _DOC


# ----------------------------------------------------------------------
# Seam 4 — API: GET returns the fields; PATCH persists them
# ----------------------------------------------------------------------

def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    from prism_service.services.task_service import TaskService

    svc = TaskService(str(tmp_path / "tasks.db"))

    class _Ctx:
        task_svc = svc

    monkeypatch.setattr(tasks_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app), svc


def test_get_task_returns_plan_fields(tmp_path, monkeypatch):
    client, svc = _client(tmp_path, monkeypatch)
    t = svc.create(title="x", plan_doc=_DOC, plan_diagram=_DIAGRAM)
    r = client.get(f"/api/tasks/{t.id}")
    assert r.status_code == 200
    task = r.json()["task"]
    assert task["plan_doc"] == _DOC
    assert task["plan_diagram"] == _DIAGRAM


def test_patch_task_persists_plan_fields(tmp_path, monkeypatch):
    client, svc = _client(tmp_path, monkeypatch)
    t = svc.create(title="x")
    r = client.patch(
        f"/api/tasks/{t.id}",
        json={"plan_doc": _DOC, "plan_diagram": _DIAGRAM},
    )
    assert r.status_code == 200, r.text
    # PATCH must not silently drop them through the kwargs filter
    g = svc.get(t.id)
    assert g.plan_doc == _DOC
    assert g.plan_diagram == _DIAGRAM
