"""Every task records the channel it came from (task b480eb15, child of
epic d6966b43) — the walking skeleton for channel provenance.

Pins, RED-first, the two persisted fields ``channel`` / ``channel_ref`` end
to end through the REAL wiring:

  (a) Task carries channel/channel_ref; TaskService.create/get round-trips
      them; a db created WITHOUT the columns migrates on open and its rows
      hydrate blank (legacy); an unknown channel is refused at create.
  (b) POST /api/tasks stores channel="ui" when none is given (channel_ref =
      the caller's session id when the body carries one), honours an
      explicit channel, refuses an unknown one with 400, and the board's
      ?fields= projection serves `channel`.
  (c) MCP task_create stores channel="mcp" with channel_ref = the request
      session id, and honours an explicit override (a slack collector
      posting over MCP passes channel="slack").
  (d) work_item_sync import stores channel=<provider> and channel_ref =
      the entity's url.
  (e) TasksPage.tsx requests `channel` in its fields projection, carries it
      on WorkItem for native AND external rows, and renders a Lozenge chip
      bound to the row's channel (the RENDERED JSX, never a comment).

The SPA has no JS test runner, so (e) asserts the actual TSX source — same
convention as tests/unit/test_tasks_page_unified_queue.py.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_TASKS_PAGE = _SRC / "pages" / "TasksPage.tsx"


def _task_svc(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext so handle_tool + the API router resolve the
    SAME tmp-backed task_svc (mirrors test_task_title_rename)."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-task-channel"
    pc._contexts.clear()


# ── (a) model + store + migration ───────────────────────────────────────

def test_channel_vocabulary_is_defined_once_on_the_model():
    from prism_service.models import task as task_model

    assert tuple(task_model.CHANNELS) == (
        "ui", "mcp", "github", "jira", "slack", "outlook", "daemon")


def test_task_defaults_to_blank_channel():
    from prism_service.models.task import Task

    t = Task(title="x")
    assert t.channel == ""
    assert t.channel_ref == ""


def test_create_round_trips_channel_and_ref(tmp_path):
    svc = _task_svc(tmp_path)
    t = svc.create(title="from slack", channel="slack",
                   channel_ref="https://slack.example/p/123")
    got = svc.get(t.id)
    assert got.channel == "slack"
    assert got.channel_ref == "https://slack.example/p/123"


def test_create_refuses_an_unknown_channel(tmp_path):
    svc = _task_svc(tmp_path)
    with pytest.raises(ValueError):
        svc.create(title="bogus", channel="carrier-pigeon")


# The tasks schema exactly as it stood BEFORE this slice (no channel columns),
# so the migration on open is exercised against a genuinely legacy db.
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
    premise_notes TEXT DEFAULT ''
);
"""


def test_legacy_db_without_channel_columns_migrates_and_hydrates_blank(tmp_path):
    db = tmp_path / "tasks.db"
    raw = sqlite3.connect(str(db))
    raw.executescript(_LEGACY_TASKS_SQL)
    raw.execute(
        "INSERT INTO tasks (id, title, created_at) VALUES (?, ?, ?)",
        ("legacy-1", "pre-channel row", "2026-01-01T00:00:00+00:00"))
    raw.commit()
    raw.close()

    from prism_service.services.task_service import TaskService
    svc = TaskService(str(db))
    cols = {r[1] for r in svc._db.execute("PRAGMA table_info(tasks)")}
    assert {"channel", "channel_ref"} <= cols
    legacy = svc.get("legacy-1")
    assert legacy is not None
    assert legacy.channel == ""
    assert legacy.channel_ref == ""
    # And a new row on the migrated db persists the fields.
    fresh = svc.create(title="post-migration", channel="daemon", channel_ref="worker-7")
    assert svc.get(fresh.id).channel == "daemon"
    assert svc.get(fresh.id).channel_ref == "worker-7"


def test_update_persists_channel_fields(tmp_path):
    svc = _task_svc(tmp_path)
    t = svc.create(title="late-stamped")
    svc.update(t.id, channel="outlook", channel_ref="msg-42")
    assert svc.get(t.id).channel == "outlook"
    assert svc.get(t.id).channel_ref == "msg-42"


# ── (b) REST: POST /api/tasks + fields projection ───────────────────────

def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_post_api_tasks_defaults_channel_to_ui(project):
    from prism_service.project_context import get_project
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "from the spa"})
    assert post.status_code in (200, 201), post.text
    tid = post.json()["task"]["id"]
    stored = get_project(project).task_svc.get(tid)
    assert stored.channel == "ui"
    assert stored.channel_ref == ""


def test_post_api_tasks_stamps_session_id_as_channel_ref(project):
    from prism_service.project_context import get_project
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "from the spa", "session_id": "sess-ui-9"})
    assert post.status_code in (200, 201), post.text
    stored = get_project(project).task_svc.get(post.json()["task"]["id"])
    assert stored.channel == "ui"
    assert stored.channel_ref == "sess-ui-9"


def test_post_api_tasks_honours_an_explicit_channel(project):
    from prism_service.project_context import get_project
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "collector", "channel": "slack",
                             "channel_ref": "https://slack.example/p/1"})
    assert post.status_code in (200, 201), post.text
    stored = get_project(project).task_svc.get(post.json()["task"]["id"])
    assert stored.channel == "slack"
    assert stored.channel_ref == "https://slack.example/p/1"


def test_post_api_tasks_refuses_an_unknown_channel(project):
    from prism_service.project_context import get_project
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "bogus", "channel": "carrier-pigeon"})
    assert post.status_code == 400, post.text
    assert get_project(project).task_svc.list() == []


def test_get_api_tasks_serialises_channel_and_projects_it(project):
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "projected", "channel": "jira",
                             "channel_ref": "https://x.atlassian.net/browse/P-1"})
    tid = post.json()["task"]["id"]

    full = client.get(f"/api/tasks/{tid}", params={"project": project}).json()["task"]
    assert full["channel"] == "jira"
    assert full["channel_ref"] == "https://x.atlassian.net/browse/P-1"

    board = client.get("/api/tasks", params={"project": project,
                                             "fields": "id,channel"}).json()["tasks"]
    assert board == [{"id": tid, "channel": "jira"}]


# ── (c) MCP task_create ─────────────────────────────────────────────────

def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def test_task_create_schema_advertises_channel_fields():
    from prism_service.mcp.tools import TOOLS
    props = {t.name: t for t in TOOLS}["task_create"].inputSchema["properties"]
    assert "channel" in props
    assert "channel_ref" in props


def test_mcp_task_create_stamps_mcp_channel_and_session_ref(project, monkeypatch):
    from prism_service.mcp import tools as mcp_tools
    from prism_service.project_context import get_project
    monkeypatch.setattr(mcp_tools, "_resolve_real_session_id", lambda: "sess-mcp-1")

    created = json.loads(_call("task_create", {"title": "via mcp"}, project))
    assert created["channel"] == "mcp"
    assert created["channel_ref"] == "sess-mcp-1"
    stored = get_project(project).task_svc.get(created["id"])
    assert stored.channel == "mcp"
    assert stored.channel_ref == "sess-mcp-1"


def test_mcp_task_create_honours_an_explicit_channel(project, monkeypatch):
    from prism_service.mcp import tools as mcp_tools
    from prism_service.project_context import get_project
    monkeypatch.setattr(mcp_tools, "_resolve_real_session_id", lambda: "sess-mcp-2")

    created = json.loads(_call(
        "task_create",
        {"title": "slack collector", "channel": "slack",
         "channel_ref": "https://slack.example/p/77"},
        project))
    stored = get_project(project).task_svc.get(created["id"])
    assert stored.channel == "slack"
    assert stored.channel_ref == "https://slack.example/p/77"


def test_mcp_task_create_refuses_an_unknown_channel(project):
    from prism_service.project_context import get_project
    out = json.loads(_call(
        "task_create", {"title": "bogus", "channel": "carrier-pigeon"}, project))
    assert "error" in out
    assert get_project(project).task_svc.list() == []


# ── (d) mirror import stamps provider + entity url ──────────────────────

_FIXTURES = _HERE.parent.parent / "fixtures" / "github"
_WS = "workspace-chan"


class _FixtureClient:
    def issues(self, connection, container, token):
        return json.loads((_FIXTURES / "issues.json").read_text(encoding="utf-8"))

    def pulls(self, connection, container, token):
        return json.loads((_FIXTURES / "pulls.json").read_text(encoding="utf-8"))


def test_github_import_stamps_provider_channel_and_entity_url(tmp_path):
    from prism_service.services.github_work import GitHubWorkAdapter
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.work_item_sync import WorkItemSyncService

    store = IntegrationStore(str(tmp_path / "integrations.db"))
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(_WS, "github", "install-1")
    cont = store.ensure_container(_WS, conn.id, "repository", "R_repo1")
    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(GitHubWorkAdapter(_FixtureClient()))
    svc.pull_container(_WS, conn, cont)

    issues = [e for e in store.list_entities(_WS) if e.entity_kind == "issue"]
    assert issues, "fixture should import at least one issue"
    imported = [t for t in tasks.list() if "external" in t.tags]
    assert imported, "each issue gets a local intake task"
    urls = {e.url for e in issues}
    for t in imported:
        assert t.channel == "github"
        assert t.channel_ref in urls, (t.title, t.channel_ref)
    # The existing provenance tags/trailer are UNTOUCHED by this slice (a
    # sibling task retires them).
    assert all("github" in t.tags for t in imported)


# ── (e) Work board source: projection + chip bound to the row's channel ──

def _page() -> str:
    return _TASKS_PAGE.read_text(encoding="utf-8")


def test_board_projection_requests_channel():
    page = _page()
    m = re.search(r"/api/tasks\?project=\$\{project\}&fields=([\w,]+)", page)
    assert m, "board fields projection not found"
    assert "channel" in m.group(1).split(",")


def test_work_item_carries_channel_for_native_and_external_rows():
    page = _page()
    # WorkItem type declares it.
    wi = re.search(r"type WorkItem = \{(.*?)\n\};", page, re.S)
    assert wi and re.search(r"^\s*channel\?: string;", wi.group(1), re.M)
    # nativeToWork maps the PERSISTED field (never the client-side `source`).
    native = re.search(r"function nativeToWork\(t: Task\): WorkItem \{(.*?)\n\}", page, re.S)
    assert native and re.search(r"^\s*channel: t\.channel,", native.group(1), re.M)
    # externalToWork renders its provider as the channel so native and
    # mirrored rows read the same way.
    ext = re.search(r"function externalToWork\(e: ExternalEntity\): WorkItem \{(.*?)\n\}", page, re.S)
    assert ext and re.search(r"^\s*channel: source,", ext.group(1), re.M)


def test_row_renders_a_lozenge_chip_bound_to_the_channel():
    page = _page()
    # The RENDERED JSX: a Lozenge guarded by the row's channel whose child
    # expression IS the channel. Parses the enclosing `{cond && <tag>}`
    # expression rather than a fixed character window, so a comment can
    # never satisfy it.
    chip = re.search(
        r"\{item\.channel\s*&&\s*\(?\s*<Lozenge\b([^>]*)>\s*\{item\.channel\}\s*</Lozenge>\s*\)?\s*\}",
        page,
    )
    assert chip, "no <Lozenge> chip rendered from item.channel"
    assert 'tone="neutral"' in chip.group(1)
