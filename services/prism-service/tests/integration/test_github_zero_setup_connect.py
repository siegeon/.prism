"""RED scaffold — GitHub connects with no setup (task f4dd3687).

Owner 2026-07-28: registering an OAuth app to read your own issues is the wrong
default for an instance running on your own machine. `gh auth status` already
reports a logged-in account with repo scope, and github_work.py:82-85 shows the
adapter only ever wanted a TOKEN STRING.

This is the WALKING SKELETON of the connect/sync rework, so the tests are
written against the REAL wiring. The ONLY thing injected anywhere in this file
is the OS process runner (the boundary to `gh` itself). In particular the
adapter registry is read exactly as production leaves it — nothing is injected
into it — because the previous epic (0784729f) shipped five green slices whose
tests each injected the collaborator that was actually missing, and the
assembled feature could not run at all.

No test here touches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

TOKEN = "gho_pretend_token_value_0123456789"
ACCOUNT = "siegeon"

_STATUS_OK = f"""github.com
  x Logged in to github.com account {ACCOUNT} (keyring)
  - Active account: true
  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'
"""


class FakeRunner:
    """Stands in for the OS boundary ONLY — `gh` itself. Everything else in
    this suite is the real production object."""

    def __init__(self, *, present=True, logged_in=True, valid=True):
        self.present, self.logged_in, self.valid = present, logged_in, valid

    def __call__(self, args):
        if not self.present:
            raise FileNotFoundError("gh")
        if args[:3] == ["gh", "auth", "status"]:
            if not self.logged_in:
                return 1, "", "You are not logged into any GitHub hosts."
            if not self.valid:
                return 1, "", "error: authentication token is invalid"
            return 0, _STATUS_OK, ""
        if args[:3] == ["gh", "auth", "token"]:
            return (0, TOKEN + "\n", "") if self.logged_in else (1, "", "no token")
        raise AssertionError(f"unexpected command: {args}")


# ── AC-1: the credential source reads what the CLI holds ──────────────

def test_the_cli_source_reports_the_token_and_account():
    from prism_service.services.github_cli_auth import GithubCliCredentials

    creds = GithubCliCredentials(runner=FakeRunner())
    assert creds.token() == TOKEN
    assert creds.account() == ACCOUNT
    # It must satisfy the interface GithubWorkAdapter already calls
    # (github_work.py:82-85) so it drops in without editing that seam.
    assert creds.installation_token(None) == TOKEN


# ── AC-2: connected is EARNED, never assumed ──────────────────────────

def test_an_invalid_token_is_not_reported_as_connected():
    """The recorded likely_misfire: saying CONNECTED because the binary is
    installed, then failing the first sync with a 401."""
    from prism_service.services.github_cli_auth import GithubCliCredentials

    state = GithubCliCredentials(runner=FakeRunner(valid=False)).status()
    assert state["state"] == "needs_attention", (
        f"a token the CLI cannot validate must NOT read connected; got "
        f"{state['state']!r}")


# ── AC-3: absence is honest and calm ──────────────────────────────────

def test_no_cli_installed_is_calm_and_names_the_fix():
    from prism_service.services.github_cli_auth import GithubCliCredentials

    state = GithubCliCredentials(runner=FakeRunner(present=False)).status()
    assert state["state"] == "not_configured"
    assert "gh" in state.get("detail", "").lower(), (
        "not_configured must name what to install rather than leaving the "
        "user guessing")


def test_cli_present_but_logged_out_is_not_connected():
    from prism_service.services.github_cli_auth import GithubCliCredentials

    state = GithubCliCredentials(runner=FakeRunner(logged_in=False)).status()
    assert state["state"] == "not_connected", (
        "logged out is a healthy state, not an error")


# ── AC-4 / AC-8 / AC-9: what the status endpoint reports ──────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    # NO OAuth app configured anywhere — that is the whole point.
    monkeypatch.delenv("PRISM_GITHUB_APP_SLUG", raising=False)
    monkeypatch.delenv("PRISM_GITHUB_CLIENT_ID", raising=False)

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


def _github_row(app):
    rows = {r["provider"]: r for r in
            app.get("/api/integrations/connect/status").json()["connectors"]}
    return rows["github"]


def test_status_reports_connected_from_the_cli_with_no_oauth_app(app, monkeypatch):
    from prism_service.services import github_cli_auth

    github_cli_auth.set_cli_credentials(
        github_cli_auth.GithubCliCredentials(runner=FakeRunner()))
    try:
        row = _github_row(app)
    finally:
        github_cli_auth.set_cli_credentials(None)

    assert row["state"] == "connected", (
        "with the GitHub CLI logged in there is nothing left to configure; "
        f"got {row['state']!r} / {row.get('detail')!r}")
    assert ACCOUNT in (row.get("account") or ""), (
        "the card must name whose account it is using")


def test_the_token_never_appears_in_a_response(app):
    from prism_service.services import github_cli_auth

    github_cli_auth.set_cli_credentials(
        github_cli_auth.GithubCliCredentials(runner=FakeRunner()))
    try:
        body = app.get("/api/integrations/connect/status").text
    finally:
        github_cli_auth.set_cli_credentials(None)
    assert TOKEN not in body, "the token must never leave the server"


def test_the_oauth_app_path_still_reports_when_there_is_no_cli(app, monkeypatch):
    """AC-9: this slice ADDS a credential origin, it does not delete one."""
    from prism_service.services import github_cli_auth

    monkeypatch.setenv("PRISM_GITHUB_APP_SLUG", "prism-test-app")
    github_cli_auth.set_cli_credentials(
        github_cli_auth.GithubCliCredentials(runner=FakeRunner(present=False)))
    try:
        row = _github_row(app)
    finally:
        github_cli_auth.set_cli_credentials(None)
    assert row["state"] in {"not_connected", "connected", "needs_attention"}, (
        "with an app slug configured, github must not fall back to "
        f"not_configured; got {row['state']!r}")


# ── AC-5 / AC-6 / AC-7: the wiring the last epic never had ────────────

def test_production_registers_a_github_adapter():
    """THE tooth. Nothing is injected here: this reads the registry exactly as
    importing the api package leaves it. On main it is empty, which is why
    every sync fails with 'no adapter for provider github'."""
    import importlib

    from prism_service.api import integrations

    integrations.reset_adapters()
    importlib.reload(integrations)

    assert "github" in integrations._adapters, (
        "no github adapter is registered in production — the surface would "
        "look alive while every sync dies, exactly as it does on main")


def test_what_is_registered_can_actually_talk_to_github():
    """AC-5 alone could be satisfied by registering a stub. The registered
    client must be REAL: the interface github_work.py:87-89 calls, aimed at
    the actual API host."""
    import importlib
    import inspect

    from prism_service.api import integrations

    integrations.reset_adapters()
    importlib.reload(integrations)
    adapter = integrations._adapters["github"]

    client = getattr(adapter, "_client", None)
    assert client is not None, "the registered adapter has no client at all"
    for method in ("issues", "pulls"):
        assert callable(getattr(client, method, None)), (
            f"the registered client cannot {method}() — github_work.py calls "
            f"it, so registration is a promise it cannot keep")
    source = inspect.getsource(type(client))
    assert "api.github.com" in source, (
        "the registered client does not target the real GitHub API; a stub "
        "registered in production is the same dead feature with a green test")


def test_sync_no_longer_fails_for_want_of_an_adapter():
    """AC-7, stated as the failure mode it replaces."""
    import importlib

    from prism_service.api import integrations

    integrations.reset_adapters()
    importlib.reload(integrations)

    providers = set(integrations._adapters)
    assert "github" in providers, (
        f"a sync would still raise 'no adapter for provider github'; "
        f"registry holds {sorted(providers)}")
