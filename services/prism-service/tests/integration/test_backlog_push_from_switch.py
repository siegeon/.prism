"""RED scaffold — push the existing backlog to GitHub (task 733af05f).

Walking-skeleton child of epic 02672417. Before this: scan_active_tasks
(work_item_sync.py:544) has zero production callers, push_task_creation
(work_item_sync.py:437) is reachable only one task at a time via the
existing /push endpoint, and set_sync (integrations_connect.py:425) writes
a preference and nothing else. So flipping the Settings sync switch on
does nothing to push a person's pre-existing backlog.

This file pins the NEW POST .../push-backlog route: preview (dry_run,
default True, creates nothing) then an explicit confirm (dry_run=False)
that creates real issues for a named subset, assigned to the connected
account, idempotent on repeat, and refused outright when sync is off.

AC-6 (production wiring) boots the REAL app and asserts the route exists
there — the shape test_task_mirror_production_wiring.py uses, because a
route that only exists inside a test's own FastAPI() instance is not a
route anyone can reach.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

REPO = "siegeon/.prism"
PROJECT = "prism-backlog-test"
ACCOUNT = "siegeon"
_STATUS_OK = f"""github.com
  x Logged in to github.com account {ACCOUNT} (keyring)
  - Token scopes: 'repo'
"""


class _FakeCliRunner:
    """The `gh` process boundary only (matches test_sync_is_opt_in.py)."""

    def __call__(self, args):
        if args[:3] == ["gh", "auth", "status"]:
            return 0, _STATUS_OK, ""
        if args[:3] == ["gh", "auth", "token"]:
            return 0, "gho_faketoken\n", ""
        raise AssertionError(f"unexpected command: {args}")


class _FakeGitHub:
    """Network boundary only — AC-6 below proves the REAL adapter is
    registered by production; this exists so a unit-shaped test does not
    open issues on a live repository."""

    provider = "github"

    def __init__(self):
        self.calls: list[tuple] = []

    def create(self, connection, container, title, body="", assignee=""):
        from prism_service.models.integration import ExternalEntityInput

        n = 9001 + len(self.calls)
        self.calls.append((title, assignee))
        return ExternalEntityInput(
            entity_kind="issue", remote_id=f"I_node_{n}",
            display_key=f"#{n}", title=title, body=body,
            url=f"https://github.com/{REPO}/issues/{n}",
            remote_status="open", status_category="open",
            remote_updated_at="2026-08-09T00:00:00Z")


def _scope():
    """The exact scope every route derives — never a guessed constant."""
    from prism_service.api import integrations_connect as connect
    from prism_service.services.auth_service import AuthService
    from prism_service.services.workspace_service import get_workspace_service

    principal = AuthService(get_workspace_service()).resolve_principal(None)
    return connect.personal_scope(principal)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A real store/outbox/workspace/task-service and the REAL connect
    router — only the GitHub network call is faked (matches the `wired`
    fixture shape in test_task_mirror_production_wiring.py)."""
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
        github_cli_auth.GithubCliCredentials(runner=_FakeCliRunner()))
    sync_prefs.set_sync_preferences(
        sync_prefs.SyncPreferences(str(tmp_path / "sync_prefs.db")))

    task_svc = TaskService(db_path=str(tmp_path / "tasks.db"), project=PROJECT)
    monkeypatch.setattr(
        project_context, "get_project",
        lambda project: type("Ctx", (), {"task_svc": task_svc})())

    fake_gh = _FakeGitHub()
    monkeypatch.setitem(integrations_api._adapters, "github", fake_gh)

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        client.task_svc = task_svc
        client.store = store
        client.fake_gh = fake_gh
        yield client

    sync_prefs.set_sync_preferences(None)
    github_cli_auth.set_cli_credentials(None)
    set_integration_store(None)
    set_workspace_service(None)
    set_outbox(None)


def _track(app):
    resp = app.put("/api/integrations/connect/github/sync",
                   json={"enabled": True})
    assert resp.status_code == 200, resp.text
    resp = app.post("/api/integrations/connect/github/track",
                    json={"repo": REPO})
    assert resp.status_code == 200, resp.text


def _seed_board(app):
    """Track the repo, turn sync on, and plant one task per bucket the
    oracle names: active (would_create), done, cancelled, and one already
    linked to a prior issue (already_linked)."""
    from prism_service.models.integration import ExternalEntityInput

    _track(app)
    svc = app.task_svc
    pending = svc.create(title="pending backlog task").id
    inprog = svc.create(title="in-progress backlog task")
    svc.update(inprog.id, status="in_progress")
    done = svc.create(title="finished long ago")
    svc.update(done.id, status="done")
    cancelled = svc.create(title="abandoned idea")
    svc.update(cancelled.id, status="cancelled")
    linked = svc.create(title="already mirrored task")

    scope = _scope()
    conn = app.store.list_connections(scope)[0]
    cont = app.store.list_containers(scope, conn.id)[0]
    entity, _ = app.store.upsert_entity(
        scope, conn.id, cont.id,
        ExternalEntityInput(entity_kind="issue", remote_id="I_prior",
                            display_key="#1",
                            url=f"https://github.com/{REPO}/issues/1"))
    link = app.store.claim_import_link(scope, entity.id, linked.id)
    app.store.activate_link(scope, link.id)

    return {"pending": pending, "in_progress": inprog.id,
            "done": done.id, "cancelled": cancelled.id,
            "linked": linked.id}


# ── AC-6: production wiring — nothing injected, boots the REAL app ─────

def test_ac6_the_route_exists_on_the_real_production_app(quiet_boot):
    """The shape test_task_mirror_production_wiring.py's AC-1/AC-2 use: if
    push-backlog is only reachable through a test's own FastAPI() instance,
    this is the test that notices."""
    from prism_service.main import app as real_app

    # main.py's own comment: FastAPI's lazy router inclusion keeps
    # include_router'd paths OUT of the top-level app.routes list (only
    # /api/sessions/{session_id} is promoted there directly) — .openapi()
    # walks the real router tree instead, so it sees what a live request
    # would actually match.
    paths = set(real_app.openapi()["paths"].keys())
    assert "/api/integrations/connect/{provider}/push-backlog" in paths, (
        "push-backlog is not mounted on the real app; a person pressing "
        "the Settings control would get a 404")

    from prism_service.api import integrations as integrations_api

    integrations_api.reset_adapters()
    integrations_api.register_builtin_adapters()
    assert "github" in integrations_api.adapters_snapshot(), (
        "the route must call through to the REAL registered adapter, not "
        "one only a test provides")


# ── AC-1 / AC-2: preview names every bucket and creates nothing ────────

def test_ac1_preview_names_would_create_and_every_skip_bucket(app):
    ids = _seed_board(app)
    resp = app.post("/api/integrations/connect/github/push-backlog",
                    params={"project": PROJECT}, json={"dry_run": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert ids["pending"] in body["would_create"]
    assert ids["in_progress"] in body["would_create"]
    assert ids["done"] in body["skipped"]["done"]
    assert ids["cancelled"] in body["skipped"]["cancelled"]
    assert ids["linked"] in body["skipped"]["already_linked"]
    assert body["created"] == []
    assert app.fake_gh.calls == [], (
        "a dry-run preview must never contact GitHub")


# ── AC-3: confirm creates real issues, assigned to the connected account ──

def test_ac3_confirm_creates_issues_assigned_to_connected_account(app):
    ids = _seed_board(app)
    resp = app.post(
        "/api/integrations/connect/github/push-backlog",
        params={"project": PROJECT},
        json={"dry_run": False, "task_ids": [ids["pending"]]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    created = body["created"]
    assert len(created) == 1 and created[0]["task_id"] == ids["pending"]
    assert created[0]["issue"] and created[0]["url"]
    assert app.fake_gh.calls[-1][1] == ACCOUNT, (
        "a pre-existing backlog task must be assigned to the connected "
        "account, not left unassigned")


# ── AC-4: idempotent — a repeat push creates no duplicate ──────────────

def test_ac4_confirming_again_reports_already_linked_not_a_duplicate(app):
    ids = _seed_board(app)
    call = dict(params={"project": PROJECT},
               json={"dry_run": False, "task_ids": [ids["pending"]]})
    app.post("/api/integrations/connect/github/push-backlog", **call)
    before = len(app.fake_gh.calls)

    resp = app.post("/api/integrations/connect/github/push-backlog", **call)
    body = resp.json()
    assert body["created"] == []
    assert ids["pending"] in body["skipped"]["already_linked"]
    assert len(app.fake_gh.calls) == before, (
        "a repeat push must create no duplicate issue")


# ── AC-5: switched off refuses and creates nothing ──────────────────────

def test_ac5_switch_off_refuses_and_creates_nothing(app):
    ids = _seed_board(app)
    app.put("/api/integrations/connect/github/sync", json={"enabled": False})

    resp = app.post(
        "/api/integrations/connect/github/push-backlog",
        params={"project": PROJECT},
        json={"dry_run": False, "task_ids": [ids["pending"]]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == []
    assert "off" in body.get("reason", "").lower()
    assert app.fake_gh.calls == [], (
        "sync off must refuse before any GitHub contact")


# ── AC-8, SUPERSEDED 2026-08-09 by epic 02672417 AC-4 (owner-approved
# plan): the switch's False->True edge IS what starts the outbound backlog
# push. The original "set_sync stays pure consent, never a trigger" claim
# (shared-contract clause 5) encoded a safety decision the owner then
# reversed at the epic's plan_gate. What remains invariant, and is pinned
# here instead: the sweep fires ONLY through the edge-triggered,
# github-only _fire_backlog_sweep guard — never inline, never on ON->ON,
# never for another provider. ──────────────────────────────────────────

def test_ac8_switch_sweep_is_edge_triggered_and_github_only():
    src = (_SERVICE_ROOT / "prism_service" / "api"
          / "integrations_connect.py").read_text(encoding="utf-8")
    start = src.index("def set_sync")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    # The raw pushers must still never be called inline from set_sync —
    # the ONLY reachable path is the guarded sweep helper.
    for forbidden in ("push_task_creation(", "scan_active_tasks("):
        assert forbidden not in body, (
            f"set_sync calls {forbidden} inline — the backlog push must go "
            "through _fire_backlog_sweep's edge/provider guard only")
    guard = re.search(
        r'if provider == "github" and not was_enabled and enabled:\s*\n'
        r'\s*_fire_backlog_sweep\(', body)
    assert guard, (
        "set_sync must fire _fire_backlog_sweep ONLY behind the "
        'github-only False->True edge guard (epic 02672417 AC-4/AC-7/AC-11)')
