"""RED scaffold — pull orchestration + API boundary (task fddfd75a).

Drives WorkItemSyncService against a scripted in-memory adapter (no provider
network) and the real FastAPI integration routes. Pins: pull is idempotent,
remote status never enters the conductor, later pulls never clobber local
edits, pagination cycles are bounded, adapter failures keep the prior cursor
and leak no secret, and every route is workspace/role scoped with the target
project validated against the workspace BEFORE any adapter runs.

Prism modules import INSIDE the helpers/fixtures so the file collects and
fails at runtime (red = rc 1) before the implementation exists.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS_A = "workspace-a"
WS_B = "workspace-b"


def _store(tmp_path, name="integrations.db"):
    from prism_service.services.integration_store import IntegrationStore

    return IntegrationStore(str(tmp_path / name))


def _scripted_adapter(provider, pages=None, *, raises=None):
    """A no-network adapter. `pages` is a list of dicts:
        {"entities": [{"remote_id":..,"title":..,"remote_status":..}, ...],
         "next_page_token": <str|None>, "next_cursor": <str|None>}
    `raises` = (code, message) to raise AdapterError on the first call.
    """
    from prism_service.services.work_item_sync import AdapterError, PulledPage
    from prism_service.models.integration import ExternalEntityInput

    script = list(pages or [])

    class _Adapter:
        def __init__(self):
            self.provider = provider
            self.calls = 0

        def pull_page(self, connection, container, cursor, page_token):
            self.calls += 1
            if raises is not None:
                raise AdapterError(raises[0], raises[1])
            idx = 0 if page_token is None else int(page_token)
            page = script[idx] if idx < len(script) else {"entities": []}
            ents = [
                ExternalEntityInput(
                    entity_kind=e.get("kind", "issue"),
                    remote_id=e["remote_id"],
                    display_key=e.get("display_key", ""),
                    title=e.get("title", ""),
                    remote_status=e.get("remote_status", "open"),
                    status_category=e.get("status_category", "open"),
                )
                for e in page.get("entities", [])
            ]
            return PulledPage(
                entities=ents,
                next_page_token=page.get("next_page_token"),
                next_cursor=page.get("next_cursor"),
            )

    return _Adapter()


def _sync(store, intake, *adapters):
    from prism_service.services.work_item_sync import WorkItemSyncService

    svc = WorkItemSyncService(store, intake=intake)
    for adapter in adapters:
        svc.register(adapter)
    return svc


def _task_svc(tmp_path, name="tasks.db"):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / name))


# ── AC-1/AC-2: pull is idempotent; imports are pending intake tasks ─────

def test_repeated_sync_is_idempotent(tmp_path):
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "repo-1")
    adapter = _scripted_adapter(
        "github",
        [{"entities": [{"remote_id": "gh-1", "title": "one"},
                       {"remote_id": "gh-2", "title": "two"}]}],
    )
    svc = _sync(store, tasks, adapter)

    svc.pull_container(WS_A, conn, cont)
    svc.pull_container(WS_A, conn, cont)

    assert len(store.list_entities(WS_A)) == 2
    assert len([t for t in tasks.list() if "external" in t.tags]) == 2
    links = store.list_links(WS_A)
    assert len(links) == 2
    assert all(link.state == "active" for link in links)


def test_remote_status_never_enters_the_conductor(tmp_path):
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "repo-1")
    adapter = _scripted_adapter(
        "github",
        [{"entities": [{"remote_id": "gh-9", "title": "done remotely",
                        "remote_status": "closed", "status_category": "done"}]}],
    )
    svc = _sync(store, tasks, adapter)
    svc.pull_container(WS_A, conn, cont)

    entity = store.list_entities(WS_A)[0]
    assert entity.remote_status == "closed"          # raw preserved
    assert entity.status_category == "done"          # normalized preserved
    link = store.list_links(WS_A, entity_id=entity.id)[0]
    task = tasks.get(link.task_id)
    assert task.status == "pending"                  # NOT "done"
    assert task.workflow_step == ""
    assert task.gate_state == "none"


def test_later_pull_does_not_clobber_local_edit(tmp_path):
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "repo-1")
    svc = _sync(store, tasks, _scripted_adapter(
        "github", [{"entities": [{"remote_id": "gh-1", "title": "remote v1"}]}]))
    svc.pull_container(WS_A, conn, cont)

    link = store.list_links(WS_A)[0]
    tasks.update(link.task_id, title="my triaged title", status="in_progress")

    # second pull, remote title changed
    svc2 = _sync(store, tasks, _scripted_adapter(
        "github", [{"entities": [{"remote_id": "gh-1", "title": "remote v2"}]}]))
    svc2.pull_container(WS_A, conn, cont)

    entity = store.list_entities(WS_A)[0]
    assert entity.title == "remote v2"               # external record updated
    task = tasks.get(link.task_id)
    assert task.title == "my triaged title"          # local edit preserved
    assert task.status == "in_progress"


# ── AC-2/AC-3: stale PR-derived tasks self-heal on the next sync (0665c829) ─
#
# A pre-fix sync used to materialize a task for every pull request too,
# keyed by the SAME deterministic task_id_for(...) a fixed sync recomputes.
# These pin that the fix retracts an untouched stale row, but never one a
# human has already started.

def test_stale_pull_request_task_is_retracted_on_next_sync(tmp_path):
    from prism_service.models.integration import task_id_for

    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "repo-1")

    stale_id = task_id_for(WS_A, conn.id, "pull_request", "PR_stale")
    tasks.ensure_external_intake(
        stale_id, title="hardening: full suite green from agent worktrees",
        tags=["github", "external"])

    adapter = _scripted_adapter("github", [{"entities": [
        {"remote_id": "PR_stale",
         "title": "hardening: full suite green from agent worktrees",
         "kind": "pull_request"},
    ]}])
    svc = _sync(store, tasks, adapter)
    svc.pull_container(WS_A, conn, cont)

    assert tasks.get(stale_id).status == "cancelled"


def test_a_touched_stale_pull_request_task_is_left_alone(tmp_path):
    from prism_service.models.integration import task_id_for

    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "repo-1")

    stale_id = task_id_for(WS_A, conn.id, "pull_request", "PR_touched")
    tasks.ensure_external_intake(
        stale_id, title="fix: thread-safe task storage for concurrent drives",
        tags=["github", "external"])
    tasks.update(stale_id, status="in_progress")

    adapter = _scripted_adapter("github", [{"entities": [
        {"remote_id": "PR_touched",
         "title": "fix: thread-safe task storage for concurrent drives",
         "kind": "pull_request"},
    ]}])
    svc = _sync(store, tasks, adapter)
    svc.pull_container(WS_A, conn, cont)

    assert tasks.get(stale_id).status == "in_progress"


# ── adapter contract: pagination cycle bounded ─────────────────────────

def test_pagination_cycle_is_detected(tmp_path):
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "repo-1")
    # every page hands back the SAME page token → an infinite loop if unguarded
    adapter = _scripted_adapter("github", [
        {"entities": [{"remote_id": "gh-1"}], "next_page_token": "0"},
    ])
    svc = _sync(store, tasks, adapter)
    run = svc.pull_container(WS_A, conn, cont)
    assert run.status == "failed"
    assert run.error_code == "pagination_cycle"
    assert adapter.calls < 100  # bounded, not spinning


# ── AC-3/AC-4: adapter failure keeps cursor, leaks no secret ───────────

def test_adapter_error_leaves_prior_cursor_and_canonical_code(tmp_path):
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "repo-1")
    store.set_cursor(WS_A, conn.id, cont.id, "default", "cursor-v1")
    adapter = _scripted_adapter("github", raises=("rate_limited", "HTTP 429"))
    svc = _sync(store, tasks, adapter)

    run = svc.pull_container(WS_A, conn, cont)
    assert run.status == "failed"
    assert run.error_code == "rate_limited"
    assert store.get_cursor(WS_A, conn.id, cont.id, "default") == "cursor-v1"


def test_secret_in_adapter_error_is_not_persisted(tmp_path):
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "repo-1")
    secret = "ghp_SUPERSECRET_TOKEN_999"
    adapter = _scripted_adapter(
        "github", raises=("adapter_error", f"auth failed with token={secret}"))
    svc = _sync(store, tasks, adapter)
    run = svc.pull_container(WS_A, conn, cont)

    assert run.error_code == "adapter_error"  # canonical, not the raw message
    # the raw secret must appear nowhere in the persisted database.
    raw = sqlite3.connect(str(tmp_path / "integrations.db"))
    try:
        dump = "\n".join(raw.iterdump())
    finally:
        raw.close()
    assert secret not in dump


# ── API boundary (team mode): workspace + role scoping, project check ───

@pytest.fixture
def team(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service import config, project_context
    from prism_service.api import auth as auth_api
    from prism_service.api import integrations as integrations_api
    from prism_service.api import projects as projects_api
    from prism_service.api import workspaces as workspaces_api
    from prism_service.services.auth_service import AuthService
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.integration_store import set_integration_store
    from prism_service.services.workspace_service import (
        WorkspaceService,
        set_workspace_service,
    )

    monkeypatch.setenv("PRISM_AUTH_MODE", "team")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", config.PROJECTS_DIR)
    project_context._contexts.clear()

    ws = WorkspaceService(tmp_path / "workspace.db")
    set_workspace_service(ws)
    store = IntegrationStore(str(tmp_path / "integrations.db"))
    set_integration_store(store)
    integrations_api.reset_adapters()

    alice = ws.create_user("alice@example.test", display_name="Alice", user_id="user-alice")
    member = ws.create_user("mem@example.test", display_name="Mem", user_id="user-mem")
    viewer = ws.create_user("view@example.test", display_name="View", user_id="user-view")
    mallory = ws.create_user("mal@example.test", display_name="Mallory", user_id="user-mal")
    ws.create_workspace("Workspace A", alice.id, workspace_id=WS_A)
    ws.create_workspace("Workspace B", mallory.id, workspace_id=WS_B)
    ws.add_membership(WS_A, member.id, "member")
    ws.add_membership(WS_A, viewer.id, "viewer")
    ws.bind_project("project-a", WS_A)
    ws.bind_project("project-b", WS_B)

    auth = AuthService(ws, mode="team")
    tokens = {
        "alice": auth.issue_token(alice.id, "t").secret,
        "member": auth.issue_token(member.id, "t").secret,
        "viewer": auth.issue_token(viewer.id, "t").secret,
        "mallory": auth.issue_token(mallory.id, "t").secret,
    }

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api/auth")
    app.include_router(workspaces_api.router, prefix="/api/workspaces")
    app.include_router(projects_api.router, prefix="/api/projects")
    app.include_router(integrations_api.router, prefix="/api/workspaces")

    with TestClient(app) as client:
        yield {
            "client": client, "store": store, "tokens": tokens,
            "integrations_api": integrations_api,
        }

    set_workspace_service(None)
    set_integration_store(None)
    integrations_api.reset_adapters()
    project_context._contexts.clear()


def _hdr(team, who):
    return {"Authorization": f"Bearer {team['tokens'][who]}"}


def test_cross_workspace_integration_read_is_denied(team):
    client = team["client"]
    # alice (owner of A) cannot read workspace B's connections.
    resp = client.get(f"/api/workspaces/{WS_B}/integrations/connections",
                      headers=_hdr(team, "alice"))
    assert resp.status_code == 403
    # anonymous is rejected too.
    assert client.get(
        f"/api/workspaces/{WS_A}/integrations/connections").status_code == 401


def test_roles_gate_connection_and_container_writes(team):
    client = team["client"]
    # viewer may read, may not create a connection (admin-only).
    assert client.get(f"/api/workspaces/{WS_A}/integrations/connections",
                      headers=_hdr(team, "viewer")).status_code == 200
    assert client.post(
        f"/api/workspaces/{WS_A}/integrations/connections",
        headers=_hdr(team, "viewer"),
        json={"provider": "github", "remote_scope": "install-1"},
    ).status_code == 403

    # admin (owner) creates the connection; unknown extra fields are rejected.
    assert client.post(
        f"/api/workspaces/{WS_A}/integrations/connections",
        headers=_hdr(team, "alice"),
        json={"provider": "github", "remote_scope": "install-1", "token": "leak"},
    ).status_code == 422

    created = client.post(
        f"/api/workspaces/{WS_A}/integrations/connections",
        headers=_hdr(team, "alice"),
        json={"provider": "github", "remote_scope": "install-1"},
    )
    assert created.status_code == 200
    conn_id = created.json()["connection"]["id"]

    # member may create a container; viewer may not.
    assert client.post(
        f"/api/workspaces/{WS_A}/integrations/containers",
        headers=_hdr(team, "viewer"),
        json={"connection_id": conn_id, "kind": "repository", "remote_id": "repo-1"},
    ).status_code == 403
    assert client.post(
        f"/api/workspaces/{WS_A}/integrations/containers",
        headers=_hdr(team, "member"),
        json={"connection_id": conn_id, "kind": "repository", "remote_id": "repo-1"},
    ).status_code == 200


def test_pull_rejects_project_not_in_workspace_before_adapter(team):
    client = team["client"]
    store = team["store"]
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "repo-1")
    adapter = _scripted_adapter("github", [{"entities": [{"remote_id": "gh-1"}]}])
    team["integrations_api"].register_adapter(adapter)

    # project-b belongs to workspace B, not the path's workspace A → 403,
    # and the adapter must never be touched.
    resp = client.post(
        f"/api/workspaces/{WS_A}/integrations/containers/{cont.id}/pull",
        params={"project": "project-b"},
        headers=_hdr(team, "alice"),
    )
    assert resp.status_code == 403
    assert adapter.calls == 0


def test_member_pull_imports_entities_as_pending_tasks(team):
    from prism_service import project_context

    client = team["client"]
    store = team["store"]
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "repo-1")
    adapter = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-1", "title": "issue one"},
        {"remote_id": "gh-2", "title": "issue two"},
    ]}])
    team["integrations_api"].register_adapter(adapter)

    resp = client.post(
        f"/api/workspaces/{WS_A}/integrations/containers/{cont.id}/pull",
        params={"project": "project-a"},
        headers=_hdr(team, "member"),
    )
    assert resp.status_code == 200
    assert adapter.calls >= 1
    intake = project_context.get_project("project-a").task_svc
    imported = [t for t in intake.list() if "external" in t.tags]
    assert len(imported) == 2
    assert all(t.status == "pending" for t in imported)
