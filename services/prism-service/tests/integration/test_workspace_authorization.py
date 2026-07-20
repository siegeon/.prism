"""Authorization oracle for the secure team boundary (task ba8abec4).

Two users and two workspaces drive the real FastAPI routers and the raw MCP
ASGI transport.  A query-string project remains a selector, never an ACL.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest


@dataclass
class TeamFixture:
    client: object
    workspace_service: object
    alice_token: str
    viewer_token: str
    mallory_token: str
    alice_id: str
    task_a_id: str
    task_b_id: str

    @staticmethod
    def bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def team(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service import config, project_context
    from prism_service.api import auth as auth_api
    from prism_service.api import projects as projects_api
    from prism_service.api import tasks as tasks_api
    from prism_service.api import workspaces as workspaces_api
    from prism_service.services.auth_service import AuthService
    from prism_service.services.workspace_service import (
        WorkspaceService,
        set_workspace_service,
    )

    monkeypatch.setenv("PRISM_AUTH_MODE", "team")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", config.PROJECTS_DIR)
    project_context._contexts.clear()

    workspace_service = WorkspaceService(tmp_path / "workspace.db")
    set_workspace_service(workspace_service)
    alice = workspace_service.create_user(
        "alice@example.test", display_name="Alice", user_id="user-alice"
    )
    viewer = workspace_service.create_user(
        "viewer@example.test", display_name="Viewer", user_id="user-viewer"
    )
    mallory = workspace_service.create_user(
        "mallory@example.test", display_name="Mallory", user_id="user-mallory"
    )
    workspace_a = workspace_service.create_workspace(
        "Workspace A", alice.id, workspace_id="workspace-a"
    )
    workspace_b = workspace_service.create_workspace(
        "Workspace B", mallory.id, workspace_id="workspace-b"
    )
    workspace_service.add_membership(workspace_a.id, viewer.id, "viewer")
    workspace_service.bind_project("project-a", workspace_a.id)
    workspace_service.bind_project("project-b", workspace_b.id)

    task_a = project_context.get_project("project-a").task_svc.create(title="A")
    task_b = project_context.get_project("project-b").task_svc.create(title="B")
    auth = AuthService(workspace_service, mode="team")
    alice_token = auth.issue_token(alice.id, "alice test").secret
    viewer_token = auth.issue_token(viewer.id, "viewer test").secret
    mallory_token = auth.issue_token(mallory.id, "mallory test").secret

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api/auth")
    app.include_router(workspaces_api.router, prefix="/api/workspaces")
    app.include_router(projects_api.router, prefix="/api/projects")
    app.include_router(tasks_api.router, prefix="/api/tasks")
    with TestClient(app) as client:
        yield TeamFixture(
            client=client,
            workspace_service=workspace_service,
            alice_token=alice_token,
            viewer_token=viewer_token,
            mallory_token=mallory_token,
            alice_id=alice.id,
            task_a_id=task_a.id,
            task_b_id=task_b.id,
        )

    set_workspace_service(None)
    project_context._contexts.clear()


def test_team_mode_rejects_anonymous_scoped_rest_calls(team):
    assert team.client.get("/api/projects").status_code == 401
    assert team.client.get("/api/tasks", params={"project": "project-a"}).status_code == 401
    assert team.client.get(
        f"/api/tasks/{team.task_a_id}", params={"project": "project-a"}
    ).status_code == 401


def test_project_lists_and_task_reads_are_membership_scoped(team):
    alice_headers = team.bearer(team.alice_token)
    projects = team.client.get("/api/projects", headers=alice_headers)
    assert projects.status_code == 200
    assert projects.json()["projects"] == ["project-a"]

    own = team.client.get(
        f"/api/tasks/{team.task_a_id}",
        params={"project": "project-a"},
        headers=alice_headers,
    )
    assert own.status_code == 200
    foreign = team.client.get(
        f"/api/tasks/{team.task_b_id}",
        params={"project": "project-b"},
        headers=alice_headers,
    )
    assert foreign.status_code == 403


def test_viewer_can_read_but_cannot_mutate_and_member_can_create(team):
    viewer_headers = team.bearer(team.viewer_token)
    assert team.client.get(
        f"/api/tasks/{team.task_a_id}",
        params={"project": "project-a"},
        headers=viewer_headers,
    ).status_code == 200
    assert team.client.post(
        "/api/tasks",
        params={"project": "project-a"},
        headers=viewer_headers,
        json={"title": "viewer must not create"},
    ).status_code == 403
    assert team.client.patch(
        f"/api/tasks/{team.task_a_id}",
        params={"project": "project-a"},
        headers=viewer_headers,
        json={"priority": 9},
    ).status_code == 403

    created = team.client.post(
        "/api/tasks",
        params={"project": "project-a"},
        headers=team.bearer(team.alice_token),
        json={"title": "owner-created task"},
    )
    assert created.status_code == 200


def test_cross_workspace_writes_return_403_before_project_resolution(team):
    response = team.client.patch(
        f"/api/tasks/{team.task_b_id}",
        params={"project": "project-b"},
        headers=team.bearer(team.alice_token),
        json={"priority": 99},
    )
    assert response.status_code == 403
    unchanged = team.client.get(
        f"/api/tasks/{team.task_b_id}",
        params={"project": "project-b"},
        headers=team.bearer(team.mallory_token),
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["task"]["priority"] != 99


def test_project_create_binds_owner_workspace_and_viewer_is_denied(team):
    denied = team.client.post(
        "/api/projects",
        headers=team.bearer(team.viewer_token),
        json={"name": "viewer-project", "workspace_id": "workspace-a"},
    )
    assert denied.status_code == 403

    created = team.client.post(
        "/api/projects",
        headers=team.bearer(team.alice_token),
        json={"name": "owner-project", "workspace_id": "workspace-a"},
    )
    assert created.status_code == 200
    assert created.json()["workspace_id"] == "workspace-a"
    assert team.workspace_service.project_workspace("owner-project").id == "workspace-a"


def test_authorized_project_cannot_be_used_to_fetch_another_projects_artifact(team):
    from prism_service.data_dir import evidence_dir, prototype_file

    (evidence_dir(team.task_b_id) / "foreign.png").write_bytes(b"not-a-real-png")
    prototype_file(team.task_b_id).write_text("<h1>foreign</h1>", encoding="utf-8")
    headers = team.bearer(team.alice_token)

    evidence = team.client.get(
        f"/api/tasks/{team.task_b_id}/evidence/foreign.png",
        params={"project": "project-a"},
        headers=headers,
    )
    prototype = team.client.get(
        f"/api/tasks/{team.task_b_id}/prototype",
        params={"project": "project-a"},
        headers=headers,
    )
    assert evidence.status_code == 404
    assert prototype.status_code == 404


def _mcp_scope(project: str, token: str | None):
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "root_path": "/mcp",
        "path": "/",
        "raw_path": b"/mcp/",
        "query_string": f"project={project}".encode(),
        "headers": headers,
        "http_version": "1.1",
    }


def _drive_mcp(monkeypatch, project: str, token: str | None):
    from prism_service.mcp import server as mcp_server
    from prism_service.mcp.request_context import get_request_context

    captured = {}

    async def fake_handle_request(scope, receive, send):
        captured["principal"] = get_request_context().principal

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        captured.setdefault("messages", []).append(message)

    monkeypatch.setattr(
        mcp_server.session_manager, "handle_request", fake_handle_request
    )
    asyncio.run(mcp_server.handle_mcp(_mcp_scope(project, token), receive, send))
    return captured


def _response_status(captured):
    starts = [m for m in captured.get("messages", []) if m["type"] == "http.response.start"]
    return starts[0]["status"] if starts else None


def test_rest_and_mcp_resolve_the_same_principal(team, monkeypatch):
    rest = team.client.get(
        "/api/auth/me", headers=team.bearer(team.alice_token)
    )
    assert rest.status_code == 200

    mcp = _drive_mcp(monkeypatch, "project-a", team.alice_token)
    assert mcp["principal"].user_id == rest.json()["user"]["id"] == team.alice_id


def test_mcp_team_mode_fails_closed_before_dispatch(team, monkeypatch):
    missing = _drive_mcp(monkeypatch, "project-a", None)
    assert _response_status(missing) == 401
    assert "principal" not in missing

    cross_workspace = _drive_mcp(monkeypatch, "project-b", team.alice_token)
    assert _response_status(cross_workspace) == 403
    assert "principal" not in cross_workspace


def test_raw_bearer_secret_is_not_returned_from_normal_endpoints(team):
    headers = team.bearer(team.alice_token)
    payloads = [
        team.client.get("/api/auth/me", headers=headers).text,
        team.client.get("/api/projects", headers=headers).text,
        team.client.get(
            "/api/tasks", params={"project": "project-a"}, headers=headers
        ).text,
    ]
    assert all(team.alice_token not in payload for payload in payloads)
