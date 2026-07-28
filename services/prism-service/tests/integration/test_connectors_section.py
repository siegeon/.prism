"""RED scaffold — Connectors is its own settings section (task e139295d).

Owner, repeatedly and correctly: "i still see the integrations in the claude
auth section". On main, SettingsPage declares no connectors section and renders
the Integrations card INSIDE section === "connections", which SECTION_META
titles "Claude auth" — so integrations are nested under Claude, and the card
there has no way to add anything.

Decision mx-dc7c38: Connectors is its OWN top-level section and Claude is a
PEER connector, not the category the others hang beneath.

Server tests drive the real status endpoint; the section/card assertions read
the ACTUAL TSX source (the SPA has no JS test runner).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SETTINGS = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
             / "SettingsPage.tsx")

STATES = {"not_configured", "not_connected", "connected", "needs_attention"}


def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


# ── AC-1 / AC-2 / AC-3: the section, the peers, the removal ───────────

def test_connectors_is_its_own_settings_section():
    src = _read(_SETTINGS)
    assert '"connectors"' in src, (
        "SectionId/SECTION_META must include a connectors section — today the "
        "nav is projects|connections|activity|logs|service|access-key")
    assert 'section === "connectors"' in src, (
        "the page must render a connectors section of its own")


def test_the_connectors_section_renders_a_card_per_connector():
    """Peer-ness is SERVER-driven: the section maps over the status rows rather
    than hardcoding providers, so a new connector needs no UI edit."""
    src = _read(_SETTINGS)
    assert "ConnectorsSection" in src
    body = src[src.index("function ConnectorsSection"):]
    assert "listConnectorStatus" in body, (
        "the section must read the server's status list")
    assert ".map(" in body, "it must render ONE CARD PER connector row"
    assert "Reconnect" in body and "Connect " in body, (
        "the action must match the state")


def test_claude_is_a_peer_connector_not_a_parent_section(app):
    """AC-2 at the level that actually decides it: the server lists Claude
    alongside GitHub and Jira, as equals."""
    rows = [r["provider"] for r in
            app.get("/api/integrations/connect/status").json()["connectors"]]
    assert {"claude", "github", "jira"} <= set(rows), (
        f"Claude must be listed as a PEER connector (mx-dc7c38); got {rows}")


def test_integrations_no_longer_render_under_claude_auth():
    """The owner's actual complaint, pinned."""
    src = _read(_SETTINGS)
    i = src.index('section === "connections"')
    # the Claude-auth block ends where the next section test begins
    nxt = src.find("section ===", i + 10)
    block = src[i:nxt if nxt > 0 else i + 4000]
    assert "IntegrationsCard" not in block, (
        "the Claude auth section must no longer render the Integrations card "
        "— that nesting is the defect")


# ── AC-4 / AC-5 / AC-7: server-reported status ────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PRISM_JIRA_CLIENT_ID", raising=False)
    monkeypatch.delenv("PRISM_GITHUB_APP_SLUG", raising=False)

    from prism_service.api import integrations_connect as connect
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store,
    )
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service,
    )

    set_workspace_service(WorkspaceService(tmp_path / "workspace.db"))
    set_integration_store(IntegrationStore(str(tmp_path / "integrations.db")))
    connect.configure_state_db(str(tmp_path / "oauth_states.db"))

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        yield client
    set_integration_store(None)
    set_workspace_service(None)


def test_status_lists_every_provider_with_a_known_state(app):
    resp = app.get("/api/integrations/connect/status")
    assert resp.status_code == 200, resp.text
    rows = {r["provider"]: r for r in resp.json()["connectors"]}
    assert {"github", "jira"} <= set(rows), (
        f"status must list every supported provider; got {sorted(rows)}")
    for provider, row in rows.items():
        assert row["state"] in STATES, (
            f"{provider} reported unknown state {row['state']!r}")


def test_unconfigured_provider_says_so_rather_than_looking_connected(app):
    """No OAuth app registered on this instance -> not_configured, calmly."""
    rows = {r["provider"]: r for r in
            app.get("/api/integrations/connect/status").json()["connectors"]}
    assert rows["jira"]["state"] == "not_configured"
    assert rows["jira"].get("detail"), (
        "not_configured must explain itself (name the env vars) instead of "
        "leaving the user guessing")


def test_zero_connections_is_a_calm_complete_page(app):
    """AC-7: nothing connected is a healthy state, never an error or a nag."""
    resp = app.get("/api/integrations/connect/status")
    assert resp.status_code == 200
    assert len(resp.json()["connectors"]) >= 2


def test_an_expired_credential_reads_needs_attention(app, tmp_path, monkeypatch):
    """AC-5: server-decided health — NOT 'a connection row exists'."""
    monkeypatch.setenv("PRISM_JIRA_CLIENT_ID", "cid")
    from prism_service.services.integration_store import get_integration_store
    from prism_service.services.jira_auth import JiraAuthStore, set_jira_auth_store

    scope = "personal-local-user"
    get_integration_store().ensure_connection(scope, "jira", "cloud-1", "acme")
    store = JiraAuthStore(str(tmp_path / "jira_auth.db"))
    store.set_connection(scope, "cloud-1", access_token="a", refresh_token="",
                         expires_at=1)  # long expired, no refresh token
    set_jira_auth_store(store)

    rows = {r["provider"]: r for r in
            app.get("/api/integrations/connect/status").json()["connectors"]}
    assert rows["jira"]["state"] == "needs_attention", (
        "an expired, unrefreshable credential must read needs_attention, not "
        "connected — otherwise it silently fails at the next sync")
    set_jira_auth_store(None)


def test_the_card_offers_reconnect_for_needs_attention():
    src = _read(_SETTINGS)
    assert "Reconnect" in src, (
        "a needs_attention connector must offer a Reconnect action")


# ── AC-6: the connect routes exist in THIS build ──────────────────────

def test_connect_routes_are_mounted_in_the_app():
    """Mount the REAL aggregate router on a real app and look at its route
    table — FastAPI's lazy inclusion keeps sub-router paths off
    api_router.routes, so inspecting that alone would false-negative."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import api_router

    probe = FastAPI()
    probe.include_router(api_router)
    # FastAPI's lazy inclusion keeps sub-router paths out of probe.routes
    # entirely (see the note in main.py), so ASK the app instead of reading
    # its table: a mounted route answers, an unmounted one 404s.
    resp = TestClient(probe).get("/api/integrations/connect/status")
    assert resp.status_code != 404, (
        "the connect routes are not mounted in this build — every button on "
        "the Connectors page would point at nothing")
