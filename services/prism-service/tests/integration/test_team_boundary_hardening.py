"""Red security oracles for task b0dad59b-9212-421c-a338-ba91df21f28b.

These tests deliberately drive ``prism_service.main.app``.  A hand-mounted
subset of routers cannot prove that the production boundary fails closed.
"""

from __future__ import annotations

import asyncio
import json
import re
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


def test_query_driven_route_does_not_trust_an_ignored_body_project(real_team):
    response = real_team.client.post(
        "/api/agent-runs/ingest",
        headers=real_team.bearer(real_team.alice_token),
        json={"project": "project-a", "run_id": "must-not-write"},
    )
    # agent-runs selects Query(default='default'); body.project is row data.
    # Alice has no access to default, so authorization must fail there.
    assert response.status_code == 403


def test_body_driven_route_authorizes_its_omitted_body_default(real_team):
    response = real_team.client.post(
        "/api/brain/ask",
        params={"project": "project-a"},
        headers=real_team.bearer(real_team.alice_token),
        json={"question": "must not reach inference"},
    )
    # brain.ask ignores the query and defaults body.project to 'default'.
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


def test_unscoped_run_detail_cannot_be_authorized_with_an_unrelated_query(
    real_team,
):
    response = real_team.client.get(
        "/api/claude-runs/foreign-run-id",
        params={"project": "project-a"},
        headers=real_team.bearer(real_team.alice_token),
    )
    assert response.status_code == 403


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


def test_failed_creator_cannot_release_a_same_workspace_success(
    real_team, monkeypatch, tmp_path
):
    from prism_service.api import projects as projects_api

    first_entered = threading.Event()
    fail_first = threading.Event()
    lock = threading.Lock()
    call_count = 0

    def project_data_dir(name: str):
        nonlocal call_count
        with lock:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_entered.set()
            fail_first.wait(timeout=3)
            raise RuntimeError("first creator failed")
        return tmp_path / name

    monkeypatch.setattr(projects_api, "project_data_dir", project_data_dir)

    def create():
        return real_team.client.post(
            "/api/projects",
            headers=real_team.bearer(real_team.alice_token),
            json={"name": "same-workspace-race", "workspace_id": "workspace-a"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(create)
        assert first_entered.wait(timeout=2)
        second = pool.submit(create)
        second_response = second.result(timeout=2)
        fail_first.set()
        first_response = first.result(timeout=3)

    assert [first_response.status_code, second_response.status_code] == [500, 200]
    ownership = real_team.service.project_workspace("same-workspace-race")
    assert ownership is not None
    assert ownership.id == "workspace-a"


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


def test_mcp_multi_workspace_admin_can_select_first_project_owner(
    real_team, monkeypatch
):
    from prism_service import project_context
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.server import call_tool, list_tools
    from prism_service.services.auth_service import AuthService

    principal = AuthService(real_team.service, mode="team").resolve_principal(
        f"Bearer {real_team.newcomer_token}"
    )
    real_team.service.create_workspace(
        "Another Empty Workspace",
        principal.user_id,
        workspace_id="workspace-empty-2",
    )
    created: list[str] = []

    def create_project(project_id: str):
        ownership = real_team.service.project_workspace(project_id)
        assert ownership is not None
        assert ownership.id == real_team.empty_workspace_id
        created.append(project_id)

    monkeypatch.setattr(project_context, "create_project", create_project)
    with use_request_context(
        PrismRequestContext(
            project_id="unbound-project", tool_profile="all", principal=principal
        )
    ):
        advertised = asyncio.run(list_tools())
        creation = asyncio.run(
            call_tool(
                "project_create",
                {
                    "project_id": "selected-first-project",
                    "workspace_id": real_team.empty_workspace_id,
                },
            )
        )

    project_create = next(tool for tool in advertised if tool.name == "project_create")
    assert "workspace_id" in project_create.inputSchema["properties"]
    assert "workspace_id" not in project_create.inputSchema["required"]
    assert creation.isError is False
    assert created == ["selected-first-project"]


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


def test_mcp_tool_argument_cannot_override_the_authorized_connection_project(
    real_team,
):
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.server import call_tool
    from prism_service.services.auth_service import AuthService

    principal = AuthService(real_team.service, mode="team").resolve_principal(
        f"Bearer {real_team.alice_token}"
    )
    with use_request_context(
        PrismRequestContext(project_id="project-a", tool_profile="all", principal=principal)
    ):
        result = asyncio.run(
            call_tool(
                "register_claude_source",
                {"project": "project-b", "cwd": "C:/foreign"},
            )
        )

    assert result.isError is True
    assert _mcp_payload(result) == {
        "error": "project selector does not match connection",
        "status": 403,
    }


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("verifier_run", {"workspace": "C:/foreign-workspace"}),
        (
            "register_claude_source",
            {"project": "project-a", "cwd": "C:/foreign-workspace"},
        ),
    ],
)
def test_team_mcp_denies_tools_with_unscoped_host_paths_before_dispatch(
    real_team, monkeypatch, tool_name, arguments
):
    from prism_service.mcp import server as server_module
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.services.auth_service import AuthService

    principal = AuthService(real_team.service, mode="team").resolve_principal(
        f"Bearer {real_team.alice_token}"
    )
    dispatched: list[str] = []

    async def dispatch(name: str, _arguments: dict, *, project_id: str):
        dispatched.append(f"{project_id}:{name}")
        raise AssertionError("unscoped host path reached MCP dispatch")

    monkeypatch.setattr(server_module, "handle_tool", dispatch)
    with use_request_context(
        PrismRequestContext(
            project_id="project-a", tool_profile="all", principal=principal
        )
    ):
        result = asyncio.run(server_module.call_tool(tool_name, arguments))

    assert result.isError is True
    assert _mcp_payload(result) == {
        "error": "tool is unavailable in team mode until its host path is scoped",
        "status": 403,
    }
    assert dispatched == []


def test_mcp_host_path_schemas_are_explicitly_classified_for_team_mode():
    from prism_service.mcp.server import _TEAM_UNSCOPED_HOST_PATH_TOOLS
    from prism_service.mcp.tools import TOOLS

    discovered: set[tuple[str, str]] = set()
    path_keys = {"cwd", "workspace"}
    path_markers = ("host filesystem", "host project directory", "working directory")

    def scan(tool_name: str, schema: dict, prefix: str = "") -> None:
        for key, child in (schema.get("properties") or {}).items():
            field = f"{prefix}.{key}" if prefix else key
            description = str(child.get("description") or "").casefold()
            if key.casefold() in path_keys or any(
                marker in description for marker in path_markers
            ):
                discovered.add((tool_name, field))
            if child.get("type") == "array" and isinstance(child.get("items"), dict):
                scan(tool_name, child["items"], f"{field}[]")
            else:
                scan(tool_name, child, field)

    for tool in TOOLS:
        scan(tool.name, tool.inputSchema)

    # project_onboard only records the supplied path as project-scoped text;
    # it never dereferences the host filesystem. Every dereferencing path tool
    # must be in the fail-closed team deny set until ownership can constrain it.
    opaque_project_metadata = {("project_onboard", "sub_projects[].path")}
    assert discovered == opaque_project_metadata | {
        ("register_claude_source", "cwd"),
        ("verifier_run", "workspace"),
    }
    assert {
        tool_name for tool_name, _field in discovered - opaque_project_metadata
    } == set(_TEAM_UNSCOPED_HOST_PATH_TOOLS)


# task 01ec3894 (commit 5d6292c) added a tool_profile kwarg to the real
# server_module.call_tool -> handle_tool call site
# (prism_service/mcp/server.py:230-233); the local `dispatch` stub below is
# widened to match that signature (task 1ab8fd0f).
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("verifier_run", {"workspace": "C:/local-workspace"}),
        (
            "register_claude_source",
            {"project": "project-a", "cwd": "C:/local-workspace"},
        ),
    ],
)
def test_local_mcp_keeps_host_path_tools_available(
    real_team, monkeypatch, tool_name, arguments
):
    from mcp.types import TextContent

    from prism_service.mcp import server as server_module
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context

    dispatched: list[str] = []

    async def dispatch(
        name: str, _arguments: dict, *, project_id: str,
        tool_profile: str = "interactive",
    ):
        dispatched.append(f"{project_id}:{name}")
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    monkeypatch.setattr(server_module, "handle_tool", dispatch)
    with use_request_context(
        PrismRequestContext(project_id="project-a", tool_profile="all")
    ):
        result = asyncio.run(server_module.call_tool(tool_name, arguments))

    assert _mcp_payload(result) == {"ok": True}
    assert dispatched == [f"project-a:{tool_name}"]


def test_local_mode_keeps_production_routes_open(real_team, monkeypatch):
    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    assert real_team.client.get(
        "/api/brain/status", params={"project": "project-a"}
    ).status_code == 200
    assert real_team.client.get("/graph/viewer/project-a").status_code == 200


# --- task 74a38571: personal host credentials stay out of team surfaces ---


def _assert_no_host_paths(dumped: str) -> None:
    """Independent oracle - deliberately NOT the production detector
    (api.security._looks_like_host_path), so a bug in the detector can't
    launder its own test green. Checks (1) no live runtime host token
    appears anywhere in the body, (2) no drive-letter or UNC path
    STRUCTURE appears anywhere - a route string like '/mcp/?project=<n>'
    never matches either shape, so this is false-positive-free."""
    from pathlib import Path

    from prism_service.config import DATA_DIR
    from prism_service.data_dir import resolve_claude_home
    from prism_service.services import github_auth as gh

    runtime_tokens = [
        str(Path.home()),
        Path.home().name,
        str(DATA_DIR),
        str(resolve_claude_home()),
        str(gh.credentials_path()),
    ]
    for token in runtime_tokens:
        if token and len(token) > 1:
            assert token not in dumped, f"host token {token!r} leaked: {dumped}"
    assert not re.search(r"[A-Za-z]:[\\/]", dumped), f"drive-letter path leaked: {dumped}"
    assert not re.search(r"\\\\[^\\/]", dumped), f"UNC path leaked: {dumped}"


def test_team_claude_auth_status_serves_no_host_path(real_team):
    from pathlib import Path

    response = real_team.client.get(
        "/api/claude-auth/status", headers=real_team.bearer(real_team.alice_token)
    )
    assert response.status_code == 200
    body = response.json()
    dumped = json.dumps(body)
    _assert_no_host_paths(dumped)
    assert Path.home().name not in dumped


def test_team_github_auth_status_serves_no_host_path(real_team):
    response = real_team.client.get(
        "/api/github-auth/status", headers=real_team.bearer(real_team.alice_token)
    )
    assert response.status_code == 200
    body = response.json()
    _assert_no_host_paths(json.dumps(body))
    assert "login" in body and "avatar_url" in body


def test_team_service_info_serves_no_host_path(real_team):
    from prism_service.__version__ import PRISM_VERSION

    response = real_team.client.get(
        "/api/service-info", headers=real_team.bearer(real_team.alice_token)
    )
    assert response.status_code == 200
    body = response.json()
    _assert_no_host_paths(json.dumps(body))
    assert body["version"] == PRISM_VERSION


def test_team_mode_keeps_the_connection_booleans(real_team):
    headers = real_team.bearer(real_team.alice_token)
    claude_body = real_team.client.get("/api/claude-auth/status", headers=headers).json()
    github_body = real_team.client.get("/api/github-auth/status", headers=headers).json()
    service_body = real_team.client.get("/api/service-info", headers=headers).json()

    assert isinstance(claude_body["authenticated"], bool)
    assert isinstance(github_body["authenticated"], bool)
    assert isinstance(service_body["claude_authenticated"], bool)
    assert isinstance(service_body["github_authenticated"], bool)


@pytest.mark.parametrize(
    "url",
    [
        "/api/claude-auth/status",
        "/api/github-auth/status",
        "/api/service-info",
    ],
)
def test_team_auth_surfaces_reject_anonymous_callers(real_team, url):
    assert real_team.client.get(url).status_code == 401


def test_local_mode_still_serves_the_developer_their_own_paths(real_team, monkeypatch):
    from prism_service.config import DATA_DIR
    from prism_service.data_dir import resolve_claude_home
    from prism_service.services import github_auth as gh

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    claude_body = real_team.client.get("/api/claude-auth/status").json()
    github_body = real_team.client.get("/api/github-auth/status").json()
    service_body = real_team.client.get("/api/service-info").json()

    home = resolve_claude_home()
    assert claude_body["config_dir"] == str(home)
    assert claude_body["credentials_path"] == str(home / ".credentials.json")
    assert github_body["credentials_path"] == str(gh.credentials_path())
    assert service_body["data_dir"] == str(DATA_DIR)
    assert service_body["claude_config_dir"] == str(home)
    assert service_body["github_credentials_path"] == str(gh.credentials_path())


def test_no_auth_only_get_surface_serves_a_host_path(real_team):
    """Structural sweep: walk every security._AUTH_ONLY_PREFIXES entry (read
    from the module, not a copied literal list) plus the two explicitly
    allowlisted github-auth GETs, so a future prefix or route is swept
    automatically with no test edit needed.

    Enumeration comes from the OpenAPI schema, NOT `app.routes`: FastAPI's lazy
    router inclusion parks every `include_router`'d path inside an
    `_IncludedRouter` whose inner routes carry UNPREFIXED paths (the prefix
    lives in the include context), so `app.routes` yields 9 entries and
    `/api/claude-auth/status` and `/api/service-info` are invisible to it.  The
    `known_surfaces` floor below exists to catch exactly that failure mode
    (`main.py:617` documents the same lazy-inclusion trap for the SPA's
    session-detail deep link)."""
    from prism_service.api.security import _AUTH_ONLY_PREFIXES

    headers = real_team.bearer(real_team.alice_token)
    prefixes = tuple(_AUTH_ONLY_PREFIXES)
    schema_paths = real_team.app.openapi()["paths"]
    candidate_paths = {
        path
        for path, operations in schema_paths.items()
        if "get" in {method.lower() for method in operations}
        and "{" not in path
        and any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in prefixes
        )
    }
    candidate_paths |= {"/api/github-auth/status", "/api/github-auth/client-id"}

    known_surfaces = {
        "/api/claude-auth/status",
        "/api/github-auth/status",
        "/api/service-info",
    }
    reached_known: set[str] = set()
    for path in sorted(candidate_paths):
        response = real_team.client.get(path, headers=headers)
        if response.status_code != 200:
            continue  # unreachable (needs a selector etc.) is not a leak
        if path in known_surfaces:
            reached_known.add(path)
        _assert_no_host_paths(json.dumps(response.json()))

    assert known_surfaces <= reached_known, (
        f"sweep never reached the known leaking surfaces: {known_surfaces - reached_known}"
    )


def test_redaction_is_structural_not_a_machine_literal(real_team, monkeypatch, tmp_path):
    """A fabricated, non-existent absolute path (not this developer's real
    home) must still be redacted, proving the detector keys on path SHAPE,
    never a hard-coded machine-specific literal."""
    import inspect

    from prism_service.api import claude_auth, github_auth, service_info

    fabricated = tmp_path / "fabricated-host" / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fabricated))

    response = real_team.client.get(
        "/api/claude-auth/status", headers=real_team.bearer(real_team.alice_token)
    )
    assert response.status_code == 200
    dumped = json.dumps(response.json())
    assert str(fabricated) not in dumped
    _assert_no_host_paths(dumped)

    for module in (claude_auth, github_auth, service_info):
        source = inspect.getsource(module)
        assert not re.search(r"[A-Za-z]:[\\\\]", source), (
            f"{module.__name__} hard-codes a drive-letter literal"
        )


# --- task 93cd2cf9: the redactor also needs POSIX host paths (Linux CI) ---


def test_redact_host_paths_strips_posix_absolute_paths():
    """CI evidence (PR #1282, run 31865514286, ubuntu runner):
    _HOST_PATH_PATTERNS only matches Windows drive-letter (C:\\) and UNC
    (\\\\host\\share) shapes, so a POSIX absolute host path never matches
    either and redact_host_paths lets it straight through. The actual
    failure captured from that run:
      AssertionError: host token '/home/runner' leaked: {"authenticated":
      false, "config_dir": "/home/runner/.claude", "credentials_path":
      "/home/runner/.claude/.credentials.json", ...}
    Pinned here with fabricated POSIX paths (never a machine literal, per
    test_redaction_is_structural_not_a_machine_literal's own contract) so
    this reproduces on ANY platform, not only a Linux runner."""
    from prism_service.api.security import redact_host_paths

    payload = {
        "config_dir": "/home/runner/.claude",
        "credentials_path": "/home/runner/.claude/.credentials.json",
        "authenticated": True,
        "login_command": "claude /login",
    }
    redacted = redact_host_paths(payload)
    assert "config_dir" not in redacted, redacted
    assert "credentials_path" not in redacted, redacted
    assert redacted["authenticated"] is True
    assert redacted["login_command"] == "claude /login"


def test_redact_host_paths_does_not_flag_api_routes_as_posix_paths():
    """Guard against an over-broad POSIX fix: a route string starts with
    '/' too but is not an absolute filesystem path, and must pass through
    untouched - this is the false-positive floor the structural fix must
    respect."""
    from prism_service.api.security import redact_host_paths

    payload = {
        "route": "/api/claude-auth/status",
        "sse_path": "/sse/live",
        "authenticated": True,
    }
    redacted = redact_host_paths(payload)
    assert redacted == payload, redacted


def test_redact_host_paths_strips_the_live_container_identifier(monkeypatch):
    """CI evidence (PR #1282, run 31865514286, ubuntu runner, re-verified on
    THIS task's own follow-up run): a GH-hosted runner's ephemeral VM
    hostname ('runnervmzvulz') is neither a drive-letter, UNC, nor
    POSIX-absolute path, so none of the three _HOST_PATH_PATTERNS catch it -
    but it starts with the exact same account-name token Path.home().name
    resolves to ('runner'), which the independent oracle
    (_assert_no_host_paths) also refuses to see leak anywhere in the body.
    Pinned with a monkeypatched host id (never a machine literal) so this
    reproduces on any platform, not only a Linux runner whose hostname
    happens to collide with its own $HOME."""
    from prism_service.api import security as sec

    monkeypatch.setattr(sec, "_raw_host_identifier", lambda: "runnervmzvulz")
    payload = {
        "authenticated": False,
        "container": "runnervmzvulz",
        "runtime": "native",
        "login_command": "claude /login",
    }
    redacted = sec.redact_host_paths(payload)
    assert "container" not in redacted, redacted
    assert redacted["authenticated"] is False
    assert redacted["login_command"] == "claude /login"
