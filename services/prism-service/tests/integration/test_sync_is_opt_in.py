"""RED scaffold — sync is a switch you control (task 01118728).

Owner: "i did not enable this to sync with github, you just appear to have
assumed that."

Task f4dd3687 made GitHub report CONNECTED from the machine's CLI token. A
green chip with no other control reads as "this is syncing", and the owner had
already said connection and sync are separate. Connected means the credential
works. Nothing else.

The same message asked for the badge to become a clickable configuration
control at the TOP RIGHT of the connection, so the thing telling you the state
is the thing you click to change it.
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

_SETTINGS = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
             / "SettingsPage.tsx")

TOKEN = "gho_pretend_token_value_0123456789"
ACCOUNT = "siegeon"
_STATUS_OK = f"""github.com
  x Logged in to github.com account {ACCOUNT} (keyring)
  - Token scopes: 'repo'
"""


class FakeRunner:
    """The `gh` process boundary only."""

    def __call__(self, args):
        if args[:3] == ["gh", "auth", "status"]:
            return 0, _STATUS_OK, ""
        if args[:3] == ["gh", "auth", "token"]:
            return 0, TOKEN + "\n", ""
        raise AssertionError(f"unexpected command: {args}")


def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _connectors_body() -> str:
    src = _read(_SETTINGS)
    return src[src.index("function ConnectorsSection"):]


@pytest.fixture
def app(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PRISM_GITHUB_APP_SLUG", raising=False)
    monkeypatch.delenv("PRISM_GITHUB_CLIENT_ID", raising=False)

    from prism_service.api import integrations_connect as connect
    from prism_service.services import github_cli_auth
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store,
    )
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service,
    )

    set_workspace_service(WorkspaceService(tmp_path / "workspace.db"))
    set_integration_store(IntegrationStore(str(tmp_path / "integrations.db")))
    connect.configure_state_db(str(tmp_path / "oauth_states.db"))
    github_cli_auth.set_cli_credentials(
        github_cli_auth.GithubCliCredentials(runner=FakeRunner()))

    # A REAL preferences store on a tmp path — the behaviour under test is the
    # default, so nothing is pre-seeded.
    from prism_service.services import sync_prefs
    sync_prefs.set_sync_preferences(
        sync_prefs.SyncPreferences(str(tmp_path / "sync_prefs.db")))

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        yield client

    sync_prefs.set_sync_preferences(None)
    github_cli_auth.set_cli_credentials(None)
    set_integration_store(None)
    set_workspace_service(None)


def _rows(app) -> dict:
    return {r["provider"]: r for r in
            app.get("/api/integrations/connect/status").json()["connectors"]}


# ── AC-1 / AC-2: nobody opted in, so nothing syncs ────────────────────

def test_nothing_syncs_until_someone_says_so(app):
    """The owner's complaint, stated as a test."""
    rows = _rows(app)
    for provider in ("github", "jira"):
        assert rows[provider].get("sync_enabled") is False, (
            f"{provider} reports sync_enabled="
            f"{rows[provider].get('sync_enabled')!r} on a fresh instance; "
            "nobody asked for it, so it must be False")


def test_connected_does_not_mean_syncing(app):
    """AC-2, and the whole point of the ticket: the credential works, which
    says nothing about consent to sync."""
    row = _rows(app)["github"]
    assert row["state"] == "connected", (
        "precondition: the CLI credential should be usable here")
    assert row["sync_enabled"] is False, (
        "a working credential must NEVER imply consent to sync")


# ── AC-3 / AC-4: the choice sticks ────────────────────────────────────

def test_enabling_sync_persists(app):
    resp = app.put("/api/integrations/connect/github/sync",
                   json={"enabled": True})
    assert resp.status_code == 200, resp.text
    assert _rows(app)["github"]["sync_enabled"] is True, (
        "the choice must survive being read back")


def test_disabling_sync_persists_and_keeps_the_connection(app):
    app.put("/api/integrations/connect/github/sync", json={"enabled": True})
    app.put("/api/integrations/connect/github/sync", json={"enabled": False})
    row = _rows(app)["github"]
    assert row["sync_enabled"] is False
    assert row["state"] == "connected", (
        "turning sync off must not disconnect the credential")


# ── AC-5: per provider ────────────────────────────────────────────────

def test_the_choice_is_per_provider(app):
    app.put("/api/integrations/connect/github/sync", json={"enabled": True})
    rows = _rows(app)
    assert rows["github"]["sync_enabled"] is True
    assert rows["jira"]["sync_enabled"] is False, (
        "enabling one provider must not enable another — the owner asked for "
        "github only, or jira only")


# ── AC-6 / AC-7: the badge is the control, top right ──────────────────

def test_the_state_badge_is_a_button_in_the_top_right_slot():
    body = _connectors_body()
    slot = body[body.index('className="shrink-0 flex items-center gap-2"'):]
    head = slot[:1200]
    assert "LABEL[c.state]" in head, (
        "the state badge must render in the card's TOP-RIGHT action slot, "
        "not inline beside the provider name")
    assert "<button" in head, "the badge must be a real control, not text"


def test_the_badge_opens_configuration_without_double_firing():
    """Recorded likely_misfire: the badge sits INSIDE the card's clickable
    header, so without stopPropagation a click opens the card and the card
    handler immediately closes it again."""
    body = _connectors_body()
    i = body.index("LABEL[c.state]")
    handler = body.rfind("onClick=", 0, i)
    assert handler != -1, "the badge has no click handler at all"
    assert "stopPropagation" in body[handler:i], (
        "the badge's onClick must stopPropagation before it changes the open "
        "state, or clicking it toggles the card twice")


# ── AC-8: the switch a person actually throws ─────────────────────────

def test_the_configuration_panel_renders_a_sync_switch():
    """Two halves, both required: the connectors card must RENDER the control,
    and the control must really be a switch. Checking only the second would
    pass on a component nothing mounts."""
    assert "<SyncSwitch" in _connectors_body(), (
        "the connector's configuration panel must render the sync switch")

    src = _read(_SETTINGS)
    switch = src[src.index("function SyncSwitch"):]
    switch = switch[:switch.index("function ConnectorsSection")]
    assert re.search(r'role="switch"', switch), (
        "the configuration needs a real switch control for syncing")
    assert "aria-checked" in switch, (
        "the switch must report its state to assistive tech")
    assert "sync" in switch.lower(), "the switch must be labelled for syncing"
    assert "setConnectorSync" in switch, (
        "flipping the switch must persist through the server, not just local "
        "state that a reload forgets")
