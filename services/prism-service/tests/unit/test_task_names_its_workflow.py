"""Task af396b2c: a task names the workflow that drives it.

Pins, RED-first, the persisted `workflow` field end to end through the REAL
wiring:

  (a) Task carries `workflow` (defaults "implement"); TaskService.create/
      update round-trip it; a db created WITHOUT the column migrates on
      open and legacy rows READ as "implement" (never blank); an unknown
      name is refused at create.
  (b) POST /api/tasks and PATCH /api/tasks/{id} accept `workflow`, refuse
      an unknown name with 400.
  (c) MCP task_create and task_update accept `workflow`, refuse an unknown
      name with an error payload.
  (d) GET /api/workflows entries carry `task_count`; the "conductor" entry
      counts active tasks whose workflow is "implement" (joined through
      models.task.WORKFLOW_ALIASES).
  (e) TaskDetailPage.tsx renders the workflow name as a neutral Lozenge
      chip (the RENDERED JSX, never a comment) — same convention as (e) in
      tests/unit/test_task_channel_provenance.py, since the SPA has no JS
      test runner.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import types
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_TASK_DETAIL_PAGE = _SRC / "pages" / "TaskDetailPage.tsx"


def _task_svc(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext so handle_tool + the API router resolve the
    SAME tmp-backed task_svc (mirrors test_task_channel_provenance)."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-task-workflow"
    pc._contexts.clear()


# ── (a) model + store + migration ───────────────────────────────────────

def test_workflow_alias_is_defined_once_on_the_model():
    from prism_service.models import task as task_model

    assert task_model.WORKFLOW_ALIASES == {"implement": "conductor"}
    assert task_model.DEFAULT_WORKFLOW == "implement"


def test_task_defaults_to_implement():
    from prism_service.models.task import Task

    t = Task(title="x")
    assert t.workflow == "implement"


def test_create_round_trips_workflow(tmp_path):
    svc = _task_svc(tmp_path)
    t = svc.create(title="from mcp", workflow="implement")
    got = svc.get(t.id)
    assert got.workflow == "implement"


def test_create_with_no_workflow_defaults_to_implement(tmp_path):
    svc = _task_svc(tmp_path)
    t = svc.create(title="no workflow given")
    assert svc.get(t.id).workflow == "implement"


def test_create_refuses_an_unknown_workflow(tmp_path):
    svc = _task_svc(tmp_path)
    with pytest.raises(ValueError):
        svc.create(title="bogus", workflow="carrier-pigeon")


# The tasks schema exactly as it stood BEFORE this slice (no workflow
# column), so the migration on open is exercised against a genuinely
# legacy db.
_LEGACY_TASKS_SQL = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    story_file TEXT DEFAULT '',
    assigned_agent TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT DEFAULT '',
    completed_at TEXT DEFAULT '',
    blocked_reason TEXT DEFAULT '',
    dependencies TEXT DEFAULT '[]',
    tags TEXT DEFAULT '[]',
    embedding BLOB,
    merge_sha TEXT,
    merged_at TEXT,
    workflow_step TEXT DEFAULT '',
    gate_state TEXT DEFAULT 'none',
    gate_reason TEXT DEFAULT '',
    parent_id TEXT DEFAULT '',
    oracle TEXT DEFAULT '',
    proof_type TEXT DEFAULT '',
    completion_proof TEXT DEFAULT '',
    likely_misfire TEXT DEFAULT '',
    full_outcome_complete INTEGER DEFAULT 0,
    allowed_files TEXT DEFAULT '[]',
    verify TEXT DEFAULT '[]',
    stop_if TEXT DEFAULT '[]',
    plan_doc TEXT DEFAULT '',
    plan_diagram TEXT DEFAULT '',
    premise_notes TEXT DEFAULT '',
    channel TEXT DEFAULT '',
    channel_ref TEXT DEFAULT ''
);
"""


def test_legacy_db_without_workflow_column_migrates_and_reads_implement(tmp_path):
    db = tmp_path / "tasks.db"
    raw = sqlite3.connect(str(db))
    raw.executescript(_LEGACY_TASKS_SQL)
    raw.execute(
        "INSERT INTO tasks (id, title, created_at) VALUES (?, ?, ?)",
        ("legacy-1", "pre-workflow row", "2026-01-01T00:00:00+00:00"))
    raw.commit()
    raw.close()

    from prism_service.services.task_service import TaskService
    svc = TaskService(str(db))
    cols = {r[1] for r in svc._db.execute("PRAGMA table_info(tasks)")}
    assert "workflow" in cols
    legacy = svc.get("legacy-1")
    assert legacy is not None
    # The raw column backfills to '' via the ALTER TABLE default, but a
    # legacy row must READ as the default driver, never blank.
    assert legacy.workflow == "implement"
    row = svc._db.execute(
        "SELECT workflow FROM tasks WHERE id=?", ("legacy-1",)).fetchone()
    assert row["workflow"] in ("", None)
    # And a new row on the migrated db persists explicitly.
    fresh = svc.create(title="post-migration")
    assert svc.get(fresh.id).workflow == "implement"


def test_update_persists_workflow(tmp_path):
    svc = _task_svc(tmp_path)
    t = svc.create(title="late-stamped")
    svc.update(t.id, workflow="implement")
    assert svc.get(t.id).workflow == "implement"


# ── (b) REST: POST /api/tasks + PATCH /api/tasks/{id} ───────────────────

def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_post_api_tasks_defaults_workflow_to_implement(project):
    from prism_service.project_context import get_project
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "from the spa"})
    assert post.status_code in (200, 201), post.text
    stored = get_project(project).task_svc.get(post.json()["task"]["id"])
    assert stored.workflow == "implement"


def test_post_api_tasks_refuses_an_unknown_workflow(project):
    from prism_service.project_context import get_project
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "bogus", "workflow": "carrier-pigeon"})
    assert post.status_code == 400, post.text
    assert get_project(project).task_svc.list() == []


def test_patch_api_tasks_can_change_workflow(project):
    from prism_service.project_context import get_project
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "will be re-driven"})
    tid = post.json()["task"]["id"]

    patch = client.patch(f"/api/tasks/{tid}", params={"project": project},
                         json={"workflow": "implement"})
    assert patch.status_code == 200, patch.text
    assert get_project(project).task_svc.get(tid).workflow == "implement"


def test_patch_api_tasks_refuses_an_unknown_workflow(project):
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "will not be re-driven"})
    tid = post.json()["task"]["id"]

    patch = client.patch(f"/api/tasks/{tid}", params={"project": project},
                         json={"workflow": "carrier-pigeon"})
    assert patch.status_code == 400, patch.text


# ── (c) MCP task_create + task_update ────────────────────────────────────

def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def test_task_create_schema_advertises_workflow_field():
    from prism_service.mcp.tools import TOOLS
    props = {t.name: t for t in TOOLS}["task_create"].inputSchema["properties"]
    assert "workflow" in props


def test_task_update_schema_advertises_workflow_field():
    from prism_service.mcp.tools import TOOLS
    props = {t.name: t for t in TOOLS}["task_update"].inputSchema["properties"]
    assert "workflow" in props


def test_mcp_task_create_defaults_workflow_to_implement(project):
    from prism_service.project_context import get_project
    created = json.loads(_call("task_create", {"title": "via mcp"}, project))
    assert created["workflow"] == "implement"
    stored = get_project(project).task_svc.get(created["id"])
    assert stored.workflow == "implement"


def test_mcp_task_create_refuses_an_unknown_workflow(project):
    from prism_service.project_context import get_project
    out = json.loads(_call(
        "task_create", {"title": "bogus", "workflow": "carrier-pigeon"}, project))
    assert "error" in out
    assert get_project(project).task_svc.list() == []


def test_mcp_task_update_can_change_workflow(project):
    from prism_service.project_context import get_project
    created = json.loads(_call("task_create", {"title": "re-driven via mcp"}, project))
    out = json.loads(_call(
        "task_update", {"id": created["id"], "workflow": "implement"}, project))
    assert "error" not in out
    stored = get_project(project).task_svc.get(created["id"])
    assert stored.workflow == "implement"


def test_mcp_task_update_refuses_an_unknown_workflow(project):
    from prism_service.project_context import get_project
    created = json.loads(_call("task_create", {"title": "guarded"}, project))
    out = json.loads(_call(
        "task_update", {"id": created["id"], "workflow": "carrier-pigeon"}, project))
    assert "error" in out
    assert get_project(project).task_svc.get(created["id"]).workflow == "implement"


# ── (d) GET /api/workflows: task_count ───────────────────────────────────

def _mk_task(**over):
    from prism_service.models.task import Task

    base = dict(
        id="t-1", title="A task", description="", status="pending",
        priority=5, assigned_agent="", updated_at="2026-08-25T00:00:00Z",
        workflow_step="", gate_state="none", parent_id="", tags=[],
    )
    base.update(over)
    return Task(**base)


class _Svc:
    """Minimal task_svc stand-in — the endpoint only ever LISTS."""

    def __init__(self, tasks):
        self.tasks = list(tasks)

    def list(self, status=None, assigned_agent=None, tag=None,
             story_file=None, parent_id=None, id=None):
        return list(self.tasks)


def _scripted_validation(project="prism"):
    return {
        "id": "validation", "name": "Build and test",
        "description": f"{project} validation", "project_type": "python+react",
        "steps": [], "bots": [], "occupancy": {},
    }


def test_workflows_catalog_entries_carry_task_count(monkeypatch):
    from prism_service.api import workflows as workflows_api

    tasks = [
        _mk_task(id="t-1", workflow="implement", status="pending"),
        _mk_task(id="t-2", workflow="implement", status="in_progress"),
        _mk_task(id="t-3", workflow="implement", status="done"),  # excluded
        _mk_task(id="t-4", workflow="", status="blocked"),  # legacy -> implement
    ]
    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc(tasks)))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        lambda project: [])

    body = workflows_api.get_workflows("prism")
    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id["conductor"]["task_count"] == 3
    assert by_id["validation"]["task_count"] == 0


def test_live_workflows_endpoint_serves_task_count(project, monkeypatch):
    from prism_service.project_context import get_project
    from prism_service.api import workflows as workflows_api
    get_project(project).task_svc.create(title="drives conductor")

    # _project_validation_workflow/_conductor_behavior_workflows round-trip
    # to the (unavailable in this test env) AosWorkflows engine — stubbed
    # exactly like test_api_workflows.py's own `_client` helper so this
    # exercises the REAL router + REAL project task_svc for task_count
    # without a network dependency.
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        lambda project: [])

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(workflows_api.router, prefix="/api/workflows")
    wf_client = TestClient(app)
    resp = wf_client.get(f"/api/workflows?project={project}")
    assert resp.status_code == 200, resp.text
    conductor = next(w for w in resp.json()["workflows"] if w["id"] == "conductor")
    assert conductor["task_count"] >= 1


# ── (e) TaskDetailPage.tsx source: rendered Lozenge chip ─────────────────

def _page() -> str:
    return _TASK_DETAIL_PAGE.read_text(encoding="utf-8")


def test_task_type_declares_workflow_field():
    page = _page()
    task_type = re.search(r"type Task = \{(.*?)\n\};", page, re.S)
    assert task_type and re.search(r"^\s*workflow\?: string;", task_type.group(1), re.M)


def test_workflow_renders_as_a_lozenge_chip_next_to_the_rail():
    page = _page()
    # The RENDERED JSX: a neutral Lozenge whose child expression falls back
    # to "implement" off task.workflow. Parses the enclosing expression
    # rather than a fixed character window, so a comment can never satisfy
    # it (same convention as test_task_channel_provenance's chip check).
    chip = re.search(
        r"<Lozenge tone=\"neutral\">\{task\.workflow \|\| \"implement\"\}</Lozenge>",
        page,
    )
    assert chip, "no <Lozenge> chip rendered from task.workflow"
