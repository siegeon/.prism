"""Agents talk on the task they are working (task bcfda588).

The approved Buzz-shaped Inbox prototype (owner: "look at the buz system")
is a conversation: "builder-02: Finished, suite is green 7 of 7. I can't
sign it off. @you take it?" This pins a TaskMessage whose author is a real
Actor (models/actor.py), never a bare string — repeating that defect is the
exact epic 0784729f distinct-actor mistake this ticket exists to avoid.

RED-FIRST: this file is committed alone, before prism_service/models/
task_message.py, prism_service/services/task_message_service.py, the
/messages routes on api/tasks.py, the api/inbox.py extension, the
task_message_post MCP verb, and the InboxPage.tsx render all exist. Imports
of those not-yet-existing modules are deferred INTO the test bodies (never
module scope) so a pre-implementation run collects cleanly and fails on
real assertions/lookups (rc=1), not a collection error (rc=2).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext (mirrors test_task_title_rename.py) so the
    API + MCP + service seams in this file all resolve the SAME tmp-backed
    task_svc / message_svc, and messages from one test never leak into
    another test's task-count assertions."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-task-messages"
    pc._contexts.clear()


def _full_client(project_id):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import inbox as inbox_api
    from prism_service.api import tasks as tasks_api

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    app.include_router(inbox_api.router, prefix="/api/inbox")
    return TestClient(app)


def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


# ---------------------------------------------------------------------
# AC-1: author is a resolved Actor, never a bare string
# ---------------------------------------------------------------------

def test_message_author_is_an_actor_not_a_bare_string(tmp_path):
    from prism_service.services.task_message_service import TaskMessageService

    svc = TaskMessageService(str(tmp_path / "task_messages.db"))
    msg = svc.post("task-1", "alice@example.com", "hello")

    assert not isinstance(msg.author, str), (
        f"author must be a resolved Actor, got a bare {type(msg.author)}")
    assert msg.author.kind and msg.author.id and msg.author.display_name is not None

    fetched = svc.list("task-1")[0]
    assert not isinstance(fetched.author, str)
    assert fetched.author.kind == msg.author.kind
    assert fetched.author.id == msg.author.id


# ---------------------------------------------------------------------
# AC-2: the one sqlite chokepoint, chronological listing
# ---------------------------------------------------------------------

def test_message_service_uses_sqlite_db_connect(tmp_path):
    import inspect

    from prism_service.services.task_message_service import TaskMessageService

    svc = TaskMessageService(str(tmp_path / "task_messages.db"))
    svc.post("task-1", "alice@example.com", "first")
    svc.post("task-1", "bob@example.com", "second")

    msgs = svc.list("task-1")
    assert [m.body for m in msgs] == ["first", "second"], (
        "messages must list oldest-first")

    src = inspect.getsource(TaskMessageService)
    assert "sqlite3.connect(" not in src, (
        "TaskMessageService must route through services/sqlite_db.connect(), "
        "never a bare sqlite3.connect() (tests/unit/test_no_bare_connect.py)")
    assert "sqlite_db.connect(" in src


# ---------------------------------------------------------------------
# AC-3: real HTTP round-trip
# ---------------------------------------------------------------------

def test_post_and_read_back_via_testclient(project):
    from prism_service.project_context import get_project

    client = _full_client(project)
    task = get_project(project).task_svc.create(title="a task getting a message")

    posted = client.post(
        f"/api/tasks/{task.id}/messages", params={"project": project},
        json={"body": "suite is green 7 of 7", "author": "builder-02"})
    assert posted.status_code == 200, posted.text
    message = posted.json()["message"]
    assert message["body"] == "suite is green 7 of 7"
    assert message["author"]["display_name"] == "builder-02"

    listed = client.get(
        f"/api/tasks/{task.id}/messages", params={"project": project})
    assert listed.status_code == 200
    got = listed.json()["messages"]
    assert any(m["id"] == message["id"] and m["body"] == message["body"]
               for m in got), got


def test_the_real_app_exposes_the_message_route(quiet_boot):
    """Through TestClient(app) from prism_service.main, never an import-time
    scan of app.routes — routers mount at STARTUP (test_inbox_surface.py's
    established convention). A distinctive 404 body ("task not found", from
    the handler) proves the route itself is wired, not merely FastAPI's
    generic not-found for an unregistered path."""
    from fastapi.testclient import TestClient

    from prism_service.main import app

    with TestClient(app) as client:
        r = client.get("/api/tasks/does-not-exist/messages",
                        params={"project": "default"})
    assert r.status_code == 404
    assert r.json().get("detail") == "task not found", (
        "the assembled app does not serve /api/tasks/{id}/messages — check "
        "the route exists on api/tasks.py's router (already mounted via "
        "api/__init__.py's include_router for tasks_router)")


# ---------------------------------------------------------------------
# AC-4: the Inbox surfaces body + author
# ---------------------------------------------------------------------

def test_inbox_includes_message_body_and_author(project):
    from prism_service.project_context import get_project

    client = _full_client(project)
    task_svc = get_project(project).task_svc
    task = task_svc.create(title="a task parked at a gate")
    task_svc.update(task.id, workflow_step="green_gate",
                     gate_state="pending", status="in_progress")

    posted = client.post(
        f"/api/tasks/{task.id}/messages", params={"project": project},
        json={"body": "I can't sign my own gate, @you take it?",
              "author": "builder-02"})
    assert posted.status_code == 200, posted.text

    body = client.get("/api/inbox", params={"project": project}).json()
    hit = [i for i in body["needs_you"] if i["task_id"] == task.id]
    assert hit, f"task not in needs_you: {body}"
    msgs = hit[0]["messages"]
    assert msgs, "the inbox entry carries no messages"
    assert msgs[0]["body"] == "I can't sign my own gate, @you take it?"
    assert msgs[0]["author"]["display_name"] == "builder-02"


# ---------------------------------------------------------------------
# AC-5: fail-closed empty for a non-member
# ---------------------------------------------------------------------

def test_membership_scoping_fails_closed_empty(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service import project_context
    from prism_service.api import inbox as inbox_api
    from prism_service.models.actor import Actor, ActorKind
    from prism_service.services.auth_service import AuthService
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service,
    )

    monkeypatch.setenv("PRISM_AUTH_MODE", "team")
    project_context._contexts.clear()

    ws = WorkspaceService(tmp_path / "workspace.db")
    set_workspace_service(ws)
    alice = ws.create_user("alice@example.test", display_name="Alice",
                            user_id="user-alice")
    mallory = ws.create_user("mallory@example.test", display_name="Mallory",
                              user_id="user-mallory")
    workspace = ws.create_workspace("Shared workspace", alice.id,
                                     workspace_id="workspace-msg-shared")
    ws.bind_project("msg-team-proj", workspace.id)

    auth = AuthService(ws, mode="team")
    mallory_token = auth.issue_token(mallory.id, "mallory").secret

    task_svc = project_context.get_project("msg-team-proj").task_svc
    message_svc = project_context.get_project("msg-team-proj").message_svc
    task = task_svc.create(title="a title mallory must never see")
    task_svc.update(task.id, workflow_step="green_gate",
                     gate_state="pending", status="in_progress")
    message_svc.post(
        task.id,
        Actor(id="agent:builder-02", kind=ActorKind.AGENT, display_name="builder-02"),
        "a secret note mallory must never see")

    app = FastAPI()
    app.include_router(inbox_api.router, prefix="/api/inbox")

    try:
        with TestClient(app) as client:
            r = client.get(
                "/api/inbox", params={"project": "msg-team-proj"},
                headers={"Authorization": f"Bearer {mallory_token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["needs_you"] == []
        assert body["activity"] == []
        assert "mallory must never see" not in r.text
    finally:
        set_workspace_service(None)
        project_context._contexts.clear()


# ---------------------------------------------------------------------
# AC-6: never mints or advances work (epic stop_if)
# ---------------------------------------------------------------------

def test_message_never_mints_or_advances_work(project):
    from prism_service.project_context import get_project

    client = _full_client(project)
    task_svc = get_project(project).task_svc
    task = task_svc.create(title="a task getting three messages")

    before_count = len(task_svc.list())
    before_step = task_svc.get(task.id).workflow_step
    before_status = task_svc.get(task.id).status

    for i in range(3):
        r = client.post(
            f"/api/tasks/{task.id}/messages", params={"project": project},
            json={"body": f"note {i}", "author": "builder-02"})
        assert r.status_code == 200, r.text
    listed = client.get(
        f"/api/tasks/{task.id}/messages", params={"project": project})
    assert len(listed.json()["messages"]) == 3

    after_count = len(task_svc.list())
    after_step = task_svc.get(task.id).workflow_step
    after_status = task_svc.get(task.id).status
    assert after_count == before_count, (
        "posting/listing messages changed the task count — a message "
        "must never mint a task")
    assert after_step == before_step, (
        "posting/listing messages advanced workflow_step")
    assert after_status == before_status


# ---------------------------------------------------------------------
# AC-7: exactly one new MCP verb
# ---------------------------------------------------------------------

def test_mcp_tool_posts_a_message(project):
    from prism_service.mcp.tools import TOOLS
    from prism_service.project_context import get_project

    names = [t.name for t in TOOLS if t.name == "task_message_post"]
    assert len(names) == 1, (
        f"task_message_post must be registered exactly once in TOOLS, "
        f"found {len(names)}")

    task = get_project(project).task_svc.create(title="a task an agent posts on")
    result = json.loads(_call(
        "task_message_post",
        {"task_id": task.id, "body": "suite is green 7 of 7",
         "author": "builder-02"},
        project))
    assert result.get("ok") is True, result

    msgs = get_project(project).message_svc.list(task.id)
    assert any(
        m.body == "suite is green 7 of 7" and m.author.display_name == "builder-02"
        for m in msgs), msgs


# ---------------------------------------------------------------------
# AC-8: InboxPage renders body + author, existing idiom only
# ---------------------------------------------------------------------

def test_inbox_page_renders_message_body_and_author():
    src = (_WEB / "pages" / "InboxPage.tsx").read_text(encoding="utf-8")
    assert "messages" in src, "InboxPage never reads item.messages"
    assert re.search(r"\.body\b", src), "InboxPage never renders a message body"
    assert "author" in src, "InboxPage never renders a message author"

    imports = re.findall(r'from "(@/[^"]+)"', src)
    allowed_prefixes = ("@/components/ui", "@/components/Lozenge", "@/lib/")
    stray = [i for i in imports if not i.startswith(allowed_prefixes)]
    assert not stray, (
        f"InboxPage imports a component outside the existing card idiom: {stray}")
