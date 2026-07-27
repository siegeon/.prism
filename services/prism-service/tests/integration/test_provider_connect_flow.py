"""RED scaffold — the provider CONNECT front door (task dbbea1d3).

Pins the missing user-facing flow: a SOLO user (local mode, no workspace, no
admin role) clicks Connect, completes the OAuth round trip, and picks which
repos / Jira projects to track — after which the ALREADY-SHIPPED sync path
imports them. Providers stay OPTIONAL (owner decision mx-639efa): with zero
connections PRISM behaves exactly as before.

Provider round trips run through an injected transport — no network.

Prism modules import INSIDE the fixture/tests so the file collects and fails at
runtime (red = rc 1) before api/integrations_connect.py exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"

CLIENT_SECRET = "jira-client-SECRET-do-not-leak"
ACCESS_TOKEN = "access-TOKEN-do-not-leak"
REFRESH_TOKEN = "refresh-TOKEN-do-not-leak"


class _FakeTransport:
    """Canned Atlassian/GitHub responses; records nothing sensitive."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url))
        return {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN,
                "expires_in": 3600}

    def get(self, url, headers=None):
        self.calls.append(("GET", url))
        return [{"id": "cloud-1", "url": "https://acme.atlassian.net",
                 "name": "acme"}]


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A LOCAL-mode app: no team, no workspace, no membership — the solo case."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import integrations_connect as connect
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store,
    )
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service,
    )

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_JIRA_CLIENT_ID", "jira-client-id")
    monkeypatch.setenv("PRISM_JIRA_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("PRISM_GITHUB_APP_SLUG", "prism-app")

    ws = WorkspaceService(tmp_path / "workspace.db")
    set_workspace_service(ws)
    store = IntegrationStore(str(tmp_path / "integrations.db"))
    set_integration_store(store)
    connect.configure_state_db(str(tmp_path / "oauth_states.db"))
    transport = _FakeTransport()
    connect.set_transport("jira", transport)
    connect.set_transport("github", transport)

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        yield {"client": client, "store": store, "ws": ws,
               "transport": transport}

    set_integration_store(None)
    set_workspace_service(None)
    connect.reset_transports()


def _read(p: Path) -> str:
    assert p.exists(), f"expected source file missing: {p}"
    return p.read_text(encoding="utf-8")


# ── AC-1: a SOLO user connects with no team ceremony ───────────────────

def test_solo_user_connects_with_no_workspace_or_admin_role(app):
    client, store, ws = app["client"], app["store"], app["ws"]
    # Precondition: this instance has NO workspaces at all.
    assert ws.list_workspaces_for_user("local-user") == []

    start = client.get("/api/integrations/connect/jira/start")
    assert start.status_code == 200, start.text
    url = start.json()["authorize_url"]
    assert "auth.atlassian.com" in url and "state=" in url
    state = url.split("state=")[1].split("&")[0]

    cb = client.get("/api/integrations/connect/jira/callback",
                    params={"code": "auth-code", "state": state},
                    follow_redirects=False)
    assert cb.status_code in (200, 302, 307), cb.text

    # EXACTLY one connection, owned by the caller's personal scope — and the
    # user never created a workspace, joined a team, or held an admin role.
    scope = "personal-local-user"
    conns = store.list_connections(scope)
    assert len(conns) == 1
    assert conns[0].provider == "jira"


def test_connections_route_lists_the_callers_own_connection(app):
    client = app["client"]
    start = client.get("/api/integrations/connect/jira/start")
    state = start.json()["authorize_url"].split("state=")[1].split("&")[0]
    client.get("/api/integrations/connect/jira/callback",
               params={"code": "c", "state": state}, follow_redirects=False)

    listed = client.get("/api/integrations/connect/connections")
    assert listed.status_code == 200
    rows = listed.json()["connections"]
    assert [r["provider"] for r in rows] == ["jira"]


# ── AC-2: OAuth state is one-time and validated ────────────────────────

def test_callback_rejects_unknown_state_and_creates_nothing(app):
    client, store = app["client"], app["store"]
    bad = client.get("/api/integrations/connect/jira/callback",
                     params={"code": "c", "state": "never-issued"},
                     follow_redirects=False)
    assert bad.status_code == 400
    assert store.list_connections("personal-local-user") == []


def test_replayed_state_is_rejected(app):
    client = app["client"]
    start = client.get("/api/integrations/connect/jira/start")
    state = start.json()["authorize_url"].split("state=")[1].split("&")[0]
    first = client.get("/api/integrations/connect/jira/callback",
                       params={"code": "c", "state": state},
                       follow_redirects=False)
    assert first.status_code in (200, 302, 307)
    replay = client.get("/api/integrations/connect/jira/callback",
                        params={"code": "c", "state": state},
                        follow_redirects=False)
    assert replay.status_code == 400


# ── AC-3: no credential ever crosses the API ───────────────────────────

def test_no_secret_or_token_in_any_response(app):
    client = app["client"]
    bodies = [client.get("/api/integrations/connect/providers").text]
    start = client.get("/api/integrations/connect/jira/start")
    bodies.append(start.text)
    state = start.json()["authorize_url"].split("state=")[1].split("&")[0]
    bodies.append(client.get("/api/integrations/connect/jira/callback",
                             params={"code": "c", "state": state},
                             follow_redirects=False).text)
    bodies.append(client.get("/api/integrations/connect/connections").text)
    for body in bodies:
        for secret in (CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN):
            assert secret not in body, f"credential leaked in: {body[:200]}"


def test_providers_route_reports_configuration_without_secrets(app):
    resp = app["client"].get("/api/integrations/connect/providers")
    assert resp.status_code == 200
    providers = {p["provider"]: p for p in resp.json()["providers"]}
    assert set(providers) == {"github", "jira"}
    assert providers["jira"]["configured"] is True   # env client id present
    assert "secret" not in json.dumps(resp.json()).lower()


# ── AC-6: the picked container reaches the SHIPPED sync path ───────────

def test_picking_a_container_binds_it_to_the_connection(app):
    client, store = app["client"], app["store"]
    start = client.get("/api/integrations/connect/jira/start")
    state = start.json()["authorize_url"].split("state=")[1].split("&")[0]
    client.get("/api/integrations/connect/jira/callback",
               params={"code": "c", "state": state}, follow_redirects=False)
    scope = "personal-local-user"
    conn = store.list_connections(scope)[0]

    picked = client.post(
        f"/api/integrations/connect/connections/{conn.id}/containers",
        json={"kind": "jira_project", "remote_id": "PROJ", "display_key": "PROJ"})
    assert picked.status_code == 200, picked.text

    containers = store.list_containers(scope, conn.id)
    assert [c.remote_id for c in containers] == ["PROJ"]


# ── AC-5: providers stay OPTIONAL (mx-639efa) ──────────────────────────

def test_zero_connections_is_a_healthy_state(app):
    """With nothing connected the surface still answers — PRISM's own tasks are
    the work of record and a provider is never required."""
    client, store = app["client"], app["store"]
    assert store.list_connections("personal-local-user") == []
    listed = client.get("/api/integrations/connect/connections")
    assert listed.status_code == 200
    assert listed.json()["connections"] == []


# ── AC-4: the UI actually offers Connect (TSX source) ──────────────────

def test_settings_renders_connect_controls_and_container_picker():
    src = _read(_WEB / "pages" / "SettingsPage.tsx")
    assert "Connect GitHub" in src, "Settings must offer Connect GitHub"
    assert "Connect Jira" in src, "Settings must offer Connect Jira"
    assert "startConnect" in src, "the button must call the start route"
    assert "addContainer" in src, "the user must pick repos / Jira projects"


def test_api_client_exposes_connect_helpers():
    src = _read(_WEB / "lib" / "api.ts")
    for fn in ("listProviders", "startConnect", "listMyConnections",
               "addContainer"):
        assert f"export async function {fn}" in src, f"api.ts must export {fn}"
    assert "/integrations/connect" in src
