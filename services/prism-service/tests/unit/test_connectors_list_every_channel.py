"""Connectors lists every channel and its state (task 64321cfe).

Settings > Connectors previously hard-coded ("claude", "github", "jira") as
the status loop. The task channel vocabulary (models.task.CHANNELS) already
names slack and outlook as remote sources with no adapter yet — this slice
makes the connector list DERIVE from that one shared vocabulary instead of
re-listing it, so a channel added to CHANNELS surfaces a card automatically,
and gives slack/outlook an honest not_configured card instead of silence.
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

_CONNECT_PY = (_SERVICE_ROOT / "prism_service" / "api" / "integrations_connect.py")
_SETTINGS = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
             / "SettingsPage.tsx")


def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def _balanced_block(src: str, open_idx: int) -> str:
    assert src[open_idx] in "([{", src[open_idx]
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] in "([{":
            depth += 1
        elif src[i] in ")]}":
            depth -= 1
            if depth == 0:
                return src[open_idx:i + 1]
    raise AssertionError(f"unbalanced bracket from index {open_idx}")


def _connectors_body() -> str:
    src = _strip_comments(_read(_SETTINGS))
    return src[src.index("function ConnectorsSection"):]


@pytest.fixture
def app(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PRISM_JIRA_CLIENT_ID", raising=False)
    monkeypatch.delenv("PRISM_GITHUB_APP_SLUG", raising=False)
    monkeypatch.delenv("PRISM_GITHUB_CLIENT_ID", raising=False)

    from prism_service.api import integrations_connect as connect
    from prism_service.services import github_cli_auth
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store,
    )
    from prism_service.services.jira_auth import JiraAuthStore, set_jira_auth_store
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service,
    )

    set_workspace_service(WorkspaceService(tmp_path / "workspace.db"))
    set_integration_store(IntegrationStore(str(tmp_path / "integrations.db")))
    set_jira_auth_store(JiraAuthStore(str(tmp_path / "jira_auth.db")))
    connect.configure_state_db(str(tmp_path / "oauth_states.db"))

    class Creds:
        def token(self): return "gho_test"
        def account(self): return "siegeon"
        def installation_token(self, _=None): return "gho_test"
        def status(self):
            return {"state": "not_configured", "detail": "no CLI", "account": ""}

    github_cli_auth.set_cli_credentials(Creds())

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        yield client

    github_cli_auth.set_cli_credentials(None)
    set_integration_store(None)
    set_jira_auth_store(None)
    set_workspace_service(None)


def _status(app) -> dict:
    return {r["provider"]: r for r in
            app.get("/api/integrations/connect/status").json()["connectors"]}


# ── API: a card per remote channel, derived from CHANNELS ─────────────

def test_status_returns_a_card_for_every_remote_channel(app):
    rows = _status(app)
    for provider in ("claude", "github", "jira", "slack", "outlook"):
        assert provider in rows, f"expected a {provider} card; got {sorted(rows)}"


def test_slack_and_outlook_are_not_configured_with_an_honest_detail(app):
    rows = _status(app)
    for provider in ("slack", "outlook"):
        row = rows[provider]
        assert row["state"] == "not_configured", row
        assert "no adapter" in row["detail"].lower(), row
        assert provider in row["detail"].lower(), row
        assert row["account"] == ""
        assert row["tracking"] == []
        assert row["sync_enabled"] is False


def test_every_card_carries_a_channel_key(app):
    from prism_service.models.task import CHANNELS

    rows = _status(app)
    for provider, row in rows.items():
        assert "channel" in row, f"{provider} card missing channel key: {row}"
    for provider in ("github", "jira", "slack", "outlook"):
        assert rows[provider]["channel"] == provider, rows[provider]
        assert rows[provider]["channel"] in CHANNELS


def test_github_and_jira_cards_are_unchanged_by_this_slice(app):
    rows = _status(app)
    assert rows["github"]["provider"] == "github"
    assert rows["jira"]["provider"] == "jira"


# ── Source: the vocabulary is IMPORTED, never re-listed ───────────────

def test_integrations_connect_imports_channels_from_models_task():
    src = _read(_CONNECT_PY)
    assert re.search(
        r"from prism_service\.models\.task import[^\n]*\bCHANNELS\b", src
    ), "expected integrations_connect.py to import CHANNELS from models.task"


def test_no_literal_channel_tuple_re_lists_the_vocabulary():
    src = _strip_comments(_read(_CONNECT_PY))
    assert '"github", "jira", "slack", "outlook"' not in src
    assert '("claude", "github", "jira")' not in src
    assert "'github', 'jira', 'slack', 'outlook'" not in src


# ── Source: SettingsPage renders every returned provider generically ──

def test_connectors_section_has_no_hardcoded_three_name_status_loop():
    src = _strip_comments(_read(_SETTINGS))
    assert 'for (const p of ["claude", "github", "jira"])' not in src
    assert '("claude", "github", "jira")' not in src


def test_connectors_section_renders_a_card_per_response_row():
    body = _connectors_body()
    assert "rows.map(" in body, (
        "expected ConnectorsSection to render one card per row returned by "
        f"the server, not a hardcoded list: {body[:400]!r}")


def test_not_configured_state_gets_no_connect_button():
    body = _connectors_body()
    i = body.index('c.state === "not_configured"')
    # No literal "Connect" button branch is gated on not_configured alone —
    # only needs_attention/not_connected render a Connect/Reconnect button;
    # not_configured has no flow to start yet (github/jira's api-token/OAuth
    # forms are a separate, existing detail-panel affordance, not this
    # button).
    window = body[max(0, i - 200):i + 200]
    assert "Connect {c.name}" not in window
