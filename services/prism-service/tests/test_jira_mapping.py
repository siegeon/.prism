"""Slice B — map a PRISM task to a Jira issue via an additive
`jira_issue_key` TEXT column (1:1, NOT a join table) — TDD red suite.

Pins the data path for the new Task.jira_issue_key field end-to-end
through the REAL seams (mirrors test_task_plan_doc's structure):

  Seam 1 (model + persistence) — TaskService.create()/update() accept and
    PERSIST jira_issue_key; row hydration reads it back from a SEPARATE
    service instance over the same tasks.db (proves on-disk, not in-memory).
  Seam 2 (additive migration) — a tasks.db created by an OLD TaskService
    that lacks jira_issue_key is migrated additively (ALTER TABLE, no row
    rewrite): PRAGMA table_info(tasks) then includes the column and a
    pre-existing row survives + backfills to ''.
  Seam 3 (model serialization) — the Task dataclass carries jira_issue_key
    and it survives serialization (dataclasses.asdict).
  Seam 4 (API) — TaskCreate/TaskUpdate accept jira_issue_key; GET returns
    it and PATCH persists it (not dropped through the kwargs filter).
  Seam 5 (MCP) — task_create / task_update schemas advertise the param and
    the dispatcher carries it through.

ALL FAIL today: the dataclass has no jira_issue_key field, the schema +
migration list lack the column, create()/update() don't accept it, the MCP
tool schemas + handlers don't pass it, and TaskCreate/TaskUpdate omit it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


_KEY = "PLAT-1"
_KEY2 = "PLAT-2"


def _svc(tmp_path, name="tasks.db"):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / name))


# ----------------------------------------------------------------------
# Seam 1 — model field + on-disk persistence round-trip
# ----------------------------------------------------------------------

def test_jira_issue_key_defaults_empty(tmp_path):
    t = _svc(tmp_path).create(title="x")
    assert t.jira_issue_key == ""


def test_create_persists_jira_issue_key_across_instances(tmp_path):
    """create(jira_issue_key=...) persists; a SEPARATE TaskService over the
    same db hydrates it back via get() — proves on-disk, not in-memory."""
    svc = _svc(tmp_path)
    t = svc.create(title="x", jira_issue_key=_KEY)
    assert t.jira_issue_key == _KEY

    fresh = _svc(tmp_path)  # new connection, same file
    assert fresh.get(t.id).jira_issue_key == _KEY
    # ...and the by-id list() read carries it too.
    assert fresh.list(id=t.id)[0].jira_issue_key == _KEY


def test_update_persists_jira_issue_key(tmp_path):
    svc = _svc(tmp_path)
    t = svc.create(title="x", jira_issue_key=_KEY)
    svc.update(t.id, jira_issue_key=_KEY2)
    assert _svc(tmp_path).get(t.id).jira_issue_key == _KEY2


# ----------------------------------------------------------------------
# Seam 2 — additive migration on a PRE-EXISTING db (no row rewrite)
# ----------------------------------------------------------------------

def test_pragma_table_info_includes_jira_issue_key_after_migration(tmp_path):
    """A tasks.db created WITHOUT jira_issue_key must migrate additively:
    PRAGMA table_info(tasks) then lists the column, the old row survives,
    and the new column backfills to ''."""
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
    assert "jira_issue_key" in cols

    legacy = svc.get("legacy-1")
    assert legacy is not None and legacy.title == "old task"
    assert legacy.jira_issue_key == ""


# ----------------------------------------------------------------------
# Seam 3 — dataclass carries the field + survives serialization
# ----------------------------------------------------------------------

def test_task_dataclass_serializes_jira_issue_key(tmp_path):
    t = _svc(tmp_path).create(title="x", jira_issue_key=_KEY)
    assert dataclasses.asdict(t)["jira_issue_key"] == _KEY


# ----------------------------------------------------------------------
# Seam 4 — API: GET returns the field; PATCH persists it
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


def test_get_task_returns_jira_issue_key(tmp_path, monkeypatch):
    client, svc = _client(tmp_path, monkeypatch)
    t = svc.create(title="x", jira_issue_key=_KEY)
    r = client.get(f"/api/tasks/{t.id}")
    assert r.status_code == 200
    assert r.json()["task"]["jira_issue_key"] == _KEY


def test_create_task_api_accepts_jira_issue_key(tmp_path, monkeypatch):
    client, svc = _client(tmp_path, monkeypatch)
    r = client.post("/api/tasks", json={"title": "x", "jira_issue_key": _KEY})
    assert r.status_code == 200, r.text
    tid = r.json()["task"]["id"]
    assert svc.get(tid).jira_issue_key == _KEY


def test_patch_task_persists_jira_issue_key(tmp_path, monkeypatch):
    client, svc = _client(tmp_path, monkeypatch)
    t = svc.create(title="x")
    r = client.patch(f"/api/tasks/{t.id}", json={"jira_issue_key": _KEY2})
    assert r.status_code == 200, r.text
    assert svc.get(t.id).jira_issue_key == _KEY2


# ----------------------------------------------------------------------
# Seam 5 — MCP dispatcher reachability (NOT just schema-defined)
# ----------------------------------------------------------------------

def _isolated_project(tmp_path, monkeypatch, pid="test-jira-mapping"):
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


def test_task_tool_schemas_advertise_jira_issue_key():
    from prism_service.mcp.tools import TOOLS
    by_name = {t.name: t for t in TOOLS}
    for tool in ("task_create", "task_update"):
        props = by_name[tool].inputSchema["properties"]
        assert "jira_issue_key" in props, f"{tool} missing jira_issue_key param"


def test_task_create_through_dispatcher_carries_jira_issue_key(tmp_path, monkeypatch):
    pid = _isolated_project(tmp_path, monkeypatch)
    created = _call("task_create", {"title": "x", "jira_issue_key": _KEY}, pid)
    assert created["jira_issue_key"] == _KEY


def test_task_update_through_dispatcher_persists_jira_issue_key(tmp_path, monkeypatch):
    pid = _isolated_project(tmp_path, monkeypatch)
    created = _call("task_create", {"title": "x"}, pid)
    tid = created["id"]
    _call("task_update", {"id": tid, "jira_issue_key": _KEY2}, pid)
    from prism_service.project_context import get_project
    assert get_project(pid).task_svc.get(tid).jira_issue_key == _KEY2
