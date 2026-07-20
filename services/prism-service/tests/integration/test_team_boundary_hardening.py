"""Red security oracles for task b0dad59b-9212-421c-a338-ba91df21f28b.

These tests deliberately drive ``prism_service.main.app``.  A hand-mounted
subset of routers cannot prove that the production boundary fails closed.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from urllib.parse import quote

import pytest


@dataclass
class RealTeam:
    app: object
    client: object
    service: object
    alice_token: str
    viewer_token: str
    mallory_token: str
    newcomer_token: str
    alice_id: str
    empty_workspace_id: str
    task_a_id: str

    @staticmethod
    def bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def real_team(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from prism_service import config, project_context
    from prism_service.api import projects as projects_api
    from prism_service.main import app
    from prism_service.services.auth_service import AuthService
    from prism_service.services.workspace_service import (
        WorkspaceService,
        set_workspace_service,
    )

    monkeypatch.setenv("PRISM_AUTH_MODE", "team")
    monkeypatch.setenv("PRISM_BOOTSTRAP_SECRET", "test-setup-secret")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", config.PROJECTS_DIR)
    project_context._contexts.clear()

    service = WorkspaceService(tmp_path / "workspace.db")
    set_workspace_service(service)
    alice = service.create_user("alice@example.test", user_id="user-alice")
    viewer = service.create_user("viewer@example.test", user_id="user-viewer")
    mallory = service.create_user("mallory@example.test", user_id="user-mallory")
    newcomer = service.create_user("new@example.test", user_id="user-new")
    workspace_a = service.create_workspace(
        "Workspace A", alice.id, workspace_id="workspace-a"
    )
    workspace_b = service.create_workspace(
        "Workspace B", mallory.id, workspace_id="workspace-b"
    )
    workspace_empty = service.create_workspace(
        "Empty Workspace", newcomer.id, workspace_id="workspace-empty"
    )
    service.add_membership(workspace_a.id, viewer.id, "viewer")
    service.add_membership(workspace_empty.id, viewer.id, "viewer")
    service.bind_project("project-a", workspace_a.id)
    service.bind_project("project-b", workspace_b.id)
    service.bind_project("prism", workspace_b.id)
    task_a = project_context.get_project("project-a").task_svc.create(title="A")

    auth = AuthService(service, mode="team")
    values = RealTeam(
        app=app,
        client=TestClient(app, raise_server_exceptions=False),
        service=service,
        alice_token=auth.issue_token(alice.id).secret,
        viewer_token=auth.issue_token(viewer.id).secret,
        mallory_token=auth.issue_token(mallory.id).secret,
        newcomer_token=auth.issue_token(newcomer.id).secret,
        alice_id=alice.id,
        empty_workspace_id=workspace_empty.id,
        task_a_id=task_a.id,
    )
    yield values

    values.client.close()
    set_workspace_service(None)
    project_context._contexts.clear()


@pytest.mark.parametrize(
    "url",
    [
        "/api/tasks?project=project-a",
        "/api/brain/status?project=project-a",
        "/api/memory/domains?project=project-a",
        "/api/conductor/state?project=project-a",
        "/api/agent-runs?project=project-a",
        "/api/sessions/missing?project=project-a",
        "/graph/viewer/project-a",
        "/graphify-visual/project-a/hierarchy.json",
    ],
)
def test_real_production_data_routes_reject_anonymous_callers(real_team, url):
    assert real_team.client.get(url).status_code == 401


@pytest.mark.parametrize(
    "url",
    [
        "/api/tasks?project=project-b",
        "/api/brain/status?project=project-b",
        "/api/memory/domains?project=project-b",
        "/api/conductor/state?project=project-b",
        "/api/agent-runs?project=project-b",
        "/api/sessions/missing?project=project-b",
        "/graph/viewer/project-b",
        "/graphify-visual/project-b/hierarchy.json",
    ],
)
def test_real_production_data_routes_reject_cross_workspace_callers(
    real_team, url
):
    response = real_team.client.get(url, headers=real_team.bearer(real_team.alice_token))
    assert response.status_code == 403


async def _asgi_get_status(app, path: str, query: str, headers=()) -> int:
    messages: list[dict] = []
    receives = iter(
        [
            {"type": "http.request", "body": b"", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        try:
            return next(receives)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": list(headers),
        "client": ("test", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert starts, messages
    return starts[0]["status"]


def test_sse_project_stream_uses_the_same_team_boundary(real_team):
    assert asyncio.run(
        _asgi_get_status(real_team.app, "/sse/sessions", "project=project-a")
    ) == 401
    assert asyncio.run(
        _asgi_get_status(
            real_team.app,
            "/sse/sessions",
            "project=project-b",
            [(b"authorization", f"Bearer {real_team.alice_token}".encode())],
        )
    ) == 403


def test_body_and_query_project_selectors_cannot_disagree(real_team):
    response = real_team.client.post(
        "/api/brain/understand",
        params={"project": "project-a"},
        headers=real_team.bearer(real_team.alice_token),
        json={"project": "project-b"},
    )
    assert response.status_code == 400


def test_route_specific_default_is_authorized_not_a_generic_default(real_team):
    # xref defaults to project='prism', not config.DEFAULT_PROJECT.  The guard
    # must authorize what the handler will actually resolve.
    response = real_team.client.get(
        "/api/xref/resolve",
        params={"token": "anything"},
        headers=real_team.bearer(real_team.alice_token),
    )
    assert response.status_code == 403


def test_team_mode_denies_process_global_mutations_before_handlers(
    real_team, monkeypatch
):
    from prism_service.api import github_auth, update

    github_called: list[bool] = []
    update_called: list[bool] = []
    monkeypatch.setattr(github_auth.gh, "clear_token", lambda: github_called.append(True))
    monkeypatch.setattr(
        update.auto_updater, "apply_update", lambda: update_called.append(True) or {"ok": True}
    )
    headers = real_team.bearer(real_team.alice_token)

    assert real_team.client.post("/api/github-auth/clear", headers=headers).status_code == 403
    assert real_team.client.post("/api/update/apply", headers=headers).status_code == 403
    assert github_called == []
    assert update_called == []


def _empty_bootstrap_client(tmp_path, monkeypatch, *, configured_secret=True):
    from fastapi.testclient import TestClient

    from prism_service.main import app
    from prism_service.services.workspace_service import WorkspaceService, set_workspace_service

    monkeypatch.setenv("PRISM_AUTH_MODE", "team")
    if configured_secret:
        monkeypatch.setenv("PRISM_BOOTSTRAP_SECRET", "configured-secret")
    else:
        monkeypatch.delenv("PRISM_BOOTSTRAP_SECRET", raising=False)
    service = WorkspaceService(tmp_path / "bootstrap.db")
    set_workspace_service(service)
    return TestClient(app, raise_server_exceptions=False), service


@pytest.mark.parametrize("supplied", [None, "wrong-secret"])
def test_bootstrap_requires_the_configured_setup_secret(tmp_path, monkeypatch, supplied):
    client, service = _empty_bootstrap_client(tmp_path, monkeypatch)
    headers = {} if supplied is None else {"X-PRISM-Bootstrap-Secret": supplied}
    response = client.post(
        "/api/auth/bootstrap",
        headers=headers,
        json={"email": "owner@example.test", "workspace_name": "Platform"},
    )
    client.close()

    assert response.status_code == 403
    assert service.has_users() is False


def test_bootstrap_fails_closed_when_no_setup_secret_is_configured(
    tmp_path, monkeypatch
):
    client, service = _empty_bootstrap_client(
        tmp_path, monkeypatch, configured_secret=False
    )
    response = client.post(
        "/api/auth/bootstrap",
        headers={"X-PRISM-Bootstrap-Secret": "anything"},
        json={"email": "owner@example.test", "workspace_name": "Platform"},
    )
    client.close()

    assert response.status_code == 503
    assert service.has_users() is False


def test_concurrent_bootstrap_yields_one_complete_owner_and_token(
    tmp_path, monkeypatch
):
    client_a, service = _empty_bootstrap_client(tmp_path, monkeypatch)
    from prism_service.main import app
    from fastapi.testclient import TestClient

    client_b = TestClient(app, raise_server_exceptions=False)
    barrier = threading.Barrier(2)

    def request(index: int):
        barrier.wait()
        client = client_a if index == 0 else client_b
        return client.post(
            "/api/auth/bootstrap",
            headers={"X-PRISM-Bootstrap-Secret": "configured-secret"},
            json={
                "email": f"owner-{index}@example.test",
                "display_name": f"Owner {index}",
                "workspace_name": f"Workspace {index}",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(request, range(2)))
    client_a.close()
    client_b.close()

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert sum(bool(response.json().get("token")) for response in responses if response.status_code == 200) == 1
    counts = {
        table: service._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("users", "workspaces", "memberships", "auth_tokens")
    }
    assert counts == {"users": 1, "workspaces": 1, "memberships": 1, "auth_tokens": 1}


@pytest.mark.parametrize(
    "project_id",
    [
        ".",
        "..",
        "../victim",
        "folder/victim",
        r"folder\victim",
        r"C:\victim",
        r"\\server\share",
        "project.",
        "Project-A",
        "CON",
    ],
)
def test_project_id_canonicalizer_rejects_filesystem_aliases(project_id):
    from prism_service.api.security import canonical_project_id

    with pytest.raises(ValueError):
        canonical_project_id(project_id)


def test_workspace_binding_rejects_encoded_backslash_alias(real_team):
    hostile = quote(r"folder\..\project-b", safe="")
    response = real_team.client.post(
        f"/api/workspaces/workspace-a/projects/{hostile}",
        headers=real_team.bearer(real_team.alice_token),
    )
    assert response.status_code == 400
    assert r"folder\..\project-b" not in real_team.service.list_projects_for_user(
        real_team.alice_id
    )


def test_project_ownership_is_reserved_before_any_directory_side_effect(
    real_team, monkeypatch, tmp_path
):
    from prism_service.api import projects as projects_api

    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    lock = threading.Lock()

    def project_data_dir(name: str):
        with lock:
            calls.append(name)
            entered.set()
        release.wait(timeout=3)
        return tmp_path / name

    monkeypatch.setattr(projects_api, "project_data_dir", project_data_dir)

    def create(token: str, workspace_id: str):
        return real_team.client.post(
            "/api/projects",
            headers=real_team.bearer(token),
            json={"name": "race-project", "workspace_id": workspace_id},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(create, real_team.alice_token, "workspace-a")
        assert entered.wait(timeout=2), "winning request never reached directory setup"
        second = pool.submit(create, real_team.mallory_token, "workspace-b")
        try:
            second_response = second.result(timeout=0.5)
        except TimeoutError:
            second_response = None
        finally:
            release.set()
        responses = [first.result(timeout=3)]
        responses.append(second_response or second.result(timeout=3))

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert calls == ["race-project"]
    assert real_team.service.project_workspace("race-project") is not None


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "t", "y"])
def test_every_pydantic_truthy_run_spelling_requires_member(
    real_team, monkeypatch, truthy
):
    from prism_service.api import tasks as tasks_api

    executed: list[bool] = []
    monkeypatch.setattr(
        tasks_api, "_run_pinned_tests", lambda *_args, **_kwargs: executed.append(True) or {}
    )
    response = real_team.client.get(
        f"/api/tasks/{real_team.task_a_id}/tests",
        params={"project": "project-a", "run": truthy},
        headers=real_team.bearer(real_team.viewer_token),
    )
    assert response.status_code == 403
    assert executed == []


def _mcp_payload(result) -> dict:
    return json.loads(result.content[0].text)


def test_mcp_admin_can_list_and_create_the_first_project(real_team, monkeypatch):
    from prism_service import project_context
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.server import call_tool
    from prism_service.services.auth_service import AuthService

    principal = AuthService(real_team.service, mode="team").resolve_principal(
        f"Bearer {real_team.newcomer_token}"
    )
    context = PrismRequestContext(
        project_id="unbound-project", tool_profile="all", principal=principal
    )
    created: list[str] = []

    def create_project(project_id: str):
        ownership = real_team.service.project_workspace(project_id)
        assert ownership is not None
        assert ownership.id == real_team.empty_workspace_id
        created.append(project_id)

    monkeypatch.setattr(project_context, "create_project", create_project)
    with use_request_context(context):
        before = asyncio.run(call_tool("project_list", {}))
        creation = asyncio.run(
            call_tool("project_create", {"project_id": "first-project"})
        )
        after = asyncio.run(call_tool("project_list", {}))

    assert _mcp_payload(before)["projects"] == []
    assert creation.isError is False
    assert created == ["first-project"]
    assert _mcp_payload(after)["projects"] == ["first-project"]


def test_mcp_viewer_cannot_create_a_project(real_team):
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.server import call_tool
    from prism_service.services.auth_service import AuthService

    principal = AuthService(real_team.service, mode="team").resolve_principal(
        f"Bearer {real_team.viewer_token}"
    )
    with use_request_context(
        PrismRequestContext(
            project_id="unbound-project", tool_profile="all", principal=principal
        )
    ):
        result = asyncio.run(call_tool("project_create", {"project_id": "nope"}))

    assert result.isError is True
    assert _mcp_payload(result) == {"error": "workspace admin access required", "status": 403}


def test_local_mode_keeps_production_routes_open(real_team, monkeypatch):
    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    assert real_team.client.get(
        "/api/brain/status", params={"project": "project-a"}
    ).status_code == 200
    assert real_team.client.get("/graph/viewer/project-a").status_code == 200
