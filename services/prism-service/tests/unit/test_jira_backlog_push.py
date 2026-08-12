"""RED scaffold -- push the existing backlog to Jira (task 56074410).

work_item_sync.py's push_task_creation/scan_active_tasks are already
provider-generic (task 88a7da0b registered a real JiraWorkAdapter). Two gaps
remain in the ROUTE, integrations_connect.py's push_backlog:

  1. The route's own preview scan (integrations_connect.py:706-712) checks
     ANY-provider active link, not the provider-scoped
     _provider_active_links (work_item_sync.py:493) that push_task_creation
     itself already uses. So a task linked to GitHub only undercounts as
     "already_linked" in a JIRA preview, even though a confirm on that same
     id would actually succeed -- the preview lies about what confirm will
     do. This file pins the FIX: provider-scoped skip buckets.
  2. The assignee default at integrations_connect.py:727-731 calls
     get_cli_credentials().status() -- GitHub-CLI-specific -- regardless of
     provider. Wrong for jira, which has no CLI identity source here. Pinned
     as a hard refusal: the fixture's CLI runner explodes if ever invoked
     for a jira push.

Mirrors test_backlog_push_from_switch.py's HTTP-level shape exactly; only
the Jira network write is faked (NFR-5), the real connect router decides
everything else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

SITE = "https://bespokelabsllc.atlassian.net"
PROJECT = "prism-jira-backlog-test"


class _ExplodingCliRunner:
    """Fails the test the moment a jira push consults the GitHub CLI -- the
    exact bug this task fixes (integrations_connect.py:727-731 misapplying
    a GitHub-specific assignee default to provider='jira')."""

    def __call__(self, args):
        raise AssertionError(
            f"a jira push-backlog must never invoke the GitHub CLI: {args}")


class _FakeJira:
    """Network boundary only -- test_ac6 below proves the REAL adapter is
    registered by production."""

    provider = "jira"

    def __init__(self):
        self.calls: list[tuple] = []

    def create(self, connection, container, title, body="", assignee=""):
        from prism_service.models.integration import ExternalEntityInput

        n = 9001 + len(self.calls)
        self.calls.append((title, assignee))
        return ExternalEntityInput(
            entity_kind="issue", remote_id=str(n),
            display_key=f"PRIS-{n}", title=title, body=body,
            url=f"{SITE}/browse/PRIS-{n}",
            remote_status="open", status_category="open",
            remote_updated_at="2026-08-11T00:00:00Z")


def _scope():
    """The exact scope every route derives -- never a guessed constant."""
    from prism_service.api import integrations_connect as connect
    from prism_service.services.auth_service import AuthService
    from prism_service.services.workspace_service import get_workspace_service

    principal = AuthService(get_workspace_service()).resolve_principal(None)
    return connect.personal_scope(principal)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A real store/outbox/workspace/task-service and the REAL connect
    router -- only the Jira network write is faked."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))

    from prism_service import project_context
    from prism_service.api import integrations as integrations_api
    from prism_service.api import integrations_connect as connect
    from prism_service.services import github_cli_auth, sync_prefs
    from prism_service.services.integration_outbox import (
        IntegrationOutbox, set_outbox)
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store)
    from prism_service.services.task_service import TaskService
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service)

    set_workspace_service(WorkspaceService(tmp_path / "workspace.db"))
    store = IntegrationStore(str(tmp_path / "integrations.db"))
    set_integration_store(store)
    outbox = IntegrationOutbox(str(tmp_path / "outbox.db"))
    set_outbox(outbox)
    connect.configure_state_db(str(tmp_path / "oauth_states.db"))
    github_cli_auth.set_cli_credentials(
        github_cli_auth.GithubCliCredentials(runner=_ExplodingCliRunner()))
    sync_prefs.set_sync_preferences(
        sync_prefs.SyncPreferences(str(tmp_path / "sync_prefs.db")))

    task_svc = TaskService(db_path=str(tmp_path / "tasks.db"), project=PROJECT)
    monkeypatch.setattr(
        project_context, "get_project",
        lambda project: type("Ctx", (), {"task_svc": task_svc})())

    fake_jira = _FakeJira()
    monkeypatch.setitem(integrations_api._adapters, "jira", fake_jira)

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        client.task_svc = task_svc
        client.store = store
        client.fake_jira = fake_jira
        yield client

    sync_prefs.set_sync_preferences(None)
    github_cli_auth.set_cli_credentials(None)
    set_integration_store(None)
    set_workspace_service(None)
    set_outbox(None)


def _track_jira(app):
    resp = app.put("/api/integrations/connect/jira/sync", json={"enabled": True})
    assert resp.status_code == 200, resp.text
    scope = _scope()
    conn = app.store.ensure_connection(scope, "jira", "cloud-1",
                                       display_name="cloud-1")
    app.store.ensure_container(scope, conn.id, "jira_project", "PRIS",
                               display_key="PRIS", display_name="PRIS",
                               url=f"{SITE}/browse/PRIS")
    return conn


def _seed_board(app):
    """Track Jira, turn sync on, and plant one task per bucket the oracle
    names -- active (would_create), done, cancelled, already linked to a
    JIRA issue (jira_linked), and a task linked ONLY to GitHub
    (github_linked_only) that must STILL be create-eligible for Jira."""
    from prism_service.models.integration import ExternalEntityInput

    conn = _track_jira(app)
    scope = _scope()
    cont = app.store.list_containers(scope, conn.id)[0]
    svc = app.task_svc

    pending = svc.create(title="pending backlog task").id
    inprog = svc.create(title="in-progress backlog task")
    svc.update(inprog.id, status="in_progress")
    done = svc.create(title="finished long ago")
    svc.update(done.id, status="done")
    cancelled = svc.create(title="abandoned idea")
    svc.update(cancelled.id, status="cancelled")
    jira_linked = svc.create(title="already mirrored to jira")

    entity, _ = app.store.upsert_entity(
        scope, conn.id, cont.id,
        ExternalEntityInput(entity_kind="issue", remote_id="10001",
                            display_key="PRIS-1", url=f"{SITE}/browse/PRIS-1"))
    link = app.store.claim_import_link(scope, entity.id, jira_linked.id)
    app.store.activate_link(scope, link.id)

    # Cross-provider: linked to GITHUB only. FR-9 (task 88a7da0b) says
    # push_task_creation itself already treats this as create-eligible for
    # jira; the route's OWN preview scan must agree (the bug this task
    # fixes), not undercount it as already_linked.
    gh_task = svc.create(title="already mirrored to github, not jira")
    gh_conn = app.store.ensure_connection(scope, "github", "install-x")
    gh_cont = app.store.ensure_container(scope, gh_conn.id, "repository",
                                         "siegeon/.prism",
                                         display_key="siegeon/.prism")
    gh_entity, _ = app.store.upsert_entity(
        scope, gh_conn.id, gh_cont.id,
        ExternalEntityInput(entity_kind="issue", remote_id="I_gh900",
                            display_key="#900",
                            url="https://github.com/siegeon/.prism/issues/900"))
    gh_link = app.store.claim_import_link(scope, gh_entity.id, gh_task.id)
    app.store.activate_link(scope, gh_link.id)

    return {"pending": pending, "in_progress": inprog.id, "done": done.id,
            "cancelled": cancelled.id, "jira_linked": jira_linked.id,
            "github_linked_only": gh_task.id}


# ── AC-1: preview is provider-scoped and creates nothing ───────────────

def test_ac1_preview_is_provider_scoped_and_creates_nothing(app):
    ids = _seed_board(app)
    resp = app.post("/api/integrations/connect/jira/push-backlog",
                    params={"project": PROJECT}, json={"dry_run": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert ids["pending"] in body["would_create"]
    assert ids["in_progress"] in body["would_create"]
    assert ids["github_linked_only"] in body["would_create"], (
        "a task linked only to GitHub must still be create-eligible for "
        "Jira -- the preview scan must be provider-scoped, not any-provider")
    assert ids["done"] in body["skipped"]["done"]
    assert ids["cancelled"] in body["skipped"]["cancelled"]
    assert ids["jira_linked"] in body["skipped"]["already_linked"]
    assert ids["github_linked_only"] not in body["skipped"]["already_linked"]
    assert body["created"] == []
    assert app.fake_jira.calls == [], "a dry-run preview must never contact Jira"


# ── AC-2: confirm creates exactly the previewed set, via the single-task verb ──

def test_ac2_confirm_creates_exactly_the_previewed_set_with_backlinks(app):
    ids = _seed_board(app)
    preview = app.post("/api/integrations/connect/jira/push-backlog",
                       params={"project": PROJECT}, json={"dry_run": True}).json()
    would = set(preview["would_create"])
    assert would == {ids["pending"], ids["in_progress"], ids["github_linked_only"]}

    resp = app.post("/api/integrations/connect/jira/push-backlog",
                    params={"project": PROJECT},
                    json={"dry_run": False, "task_ids": preview["would_create"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    created_ids = {c["task_id"] for c in body["created"]}
    assert created_ids == would
    for c in body["created"]:
        assert c["url"].startswith(f"{SITE}/browse/"), c
    assert len(app.fake_jira.calls) == len(would), (
        "confirm must call the existing single-task push_task_creation once "
        "per would-create id, never a new bulk-push code path")


# ── AC-3: re-preview after confirm reports 0 would-create ──────────────

def test_ac3_re_preview_after_confirm_reports_zero(app):
    ids = _seed_board(app)
    preview = app.post("/api/integrations/connect/jira/push-backlog",
                       params={"project": PROJECT}, json={"dry_run": True}).json()
    app.post("/api/integrations/connect/jira/push-backlog",
            params={"project": PROJECT},
            json={"dry_run": False, "task_ids": preview["would_create"]})

    resp = app.post("/api/integrations/connect/jira/push-backlog",
                    params={"project": PROJECT}, json={"dry_run": True})
    body = resp.json()
    assert body["would_create"] == [], (
        "preview and confirm must compute from the identical eligibility "
        "set -- everything just pushed must now read already_linked")
    for task_id in (ids["pending"], ids["in_progress"], ids["github_linked_only"]):
        assert task_id in body["skipped"]["already_linked"]


# ── AC-4: sync off refuses, creates nothing ─────────────────────────────

def test_ac4_switch_off_refuses_and_creates_nothing(app):
    ids = _seed_board(app)
    app.put("/api/integrations/connect/jira/sync", json={"enabled": False})
    resp = app.post("/api/integrations/connect/jira/push-backlog",
                    params={"project": PROJECT},
                    json={"dry_run": False, "task_ids": [ids["pending"]]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == []
    assert "off" in body.get("reason", "").lower()
    assert app.fake_jira.calls == []


# ── AC-5: assignee default never touches the GitHub CLI for jira ───────

def test_ac5_confirm_never_consults_the_github_cli_for_jira(app):
    """The bug this task fixes: integrations_connect.py:727-731 called
    get_cli_credentials().status() unconditionally. The fixture's CLI
    runner explodes if invoked at all -- reaching this far without an
    AssertionError from _ExplodingCliRunner IS the proof."""
    ids = _seed_board(app)
    resp = app.post("/api/integrations/connect/jira/push-backlog",
                    params={"project": PROJECT},
                    json={"dry_run": False, "task_ids": [ids["pending"]]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"], body
    assert app.fake_jira.calls[-1][1] == "", (
        "jira has no CLI identity source here; an unspecified assignee "
        "must stay empty, never borrow the GitHub CLI account")


# ── AC-6: production wiring -- nothing injected, boots the REAL app ────

def test_ac6_the_route_exists_on_the_real_app_with_jira_registered(quiet_boot):
    from prism_service.main import app as real_app

    paths = set(real_app.openapi()["paths"].keys())
    assert "/api/integrations/connect/{provider}/push-backlog" in paths, (
        "push-backlog is not mounted on the real app; a person pressing "
        "the Jira card's control would get a 404")

    from prism_service.api import integrations as integrations_api

    integrations_api.reset_adapters()
    integrations_api.register_builtin_adapters()
    assert "jira" in integrations_api.adapters_snapshot(), (
        "the route must call through to a REAL registered jira adapter, "
        "not one only a test provides")


# ── AC-7: a Jira-origin (imported) task never re-exports itself ────────

def test_ac7_jira_origin_imported_task_never_reexports_its_own_issue(app):
    ids = _seed_board(app)
    resp = app.post("/api/integrations/connect/jira/push-backlog",
                    params={"project": PROJECT}, json={"dry_run": True})
    body = resp.json()
    assert ids["jira_linked"] not in body["would_create"]
    assert ids["jira_linked"] in body["skipped"]["already_linked"]


# ── Regression (live preview 2026-08-11): a GITHUB-IMPORTED task must
# never preview as jira-eligible. On the owner's real board the first live
# jira preview offered would_create=48, ALL of them external-tagged tasks
# imported FROM GitHub - the observer path refuses IMPORTED_TAGS, but the
# backlog route consulted only status + this-provider links. Imported
# tasks belong in their own skip bucket, and a confirm naming one
# explicitly must still create nothing.
def test_imported_tasks_never_preview_or_push_cross_provider(app):
    ids = _seed_board(app)
    svc = app.task_svc
    imported = svc.create(title="Imported from GitHub, do not re-export",
                          tags=["external", "github"])

    resp = app.post("/api/integrations/connect/jira/push-backlog",
                    params={"project": PROJECT}, json={"dry_run": True})
    body = resp.json()
    assert imported.id not in body["would_create"], (
        "an external-tagged task previewed as jira-eligible - the exact "
        "48-task offer the live preview made on 2026-08-11")
    assert imported.id in body["skipped"].get("imported", []), (
        "imported tasks need their OWN skip bucket so the preview says "
        "what happened to them")

    resp = app.post(
        "/api/integrations/connect/jira/push-backlog",
        params={"project": PROJECT},
        json={"dry_run": False, "task_ids": [imported.id]})
    body = resp.json()
    assert body["created"] == [], (
        "a confirm explicitly naming an imported task must refuse it")
