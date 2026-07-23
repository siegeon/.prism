"""RED scaffold — Atlassian OAuth 3LO lifecycle (task fbe9f26c).

Pins the not-yet-built jira_oauth + jira_auth: one-time workspace-bound state,
cloudId discovery, and access-token expiry/refresh that uses the refresh token
as the refresh GRANT (never as a Bearer). Deterministic — a fake transport
records every call; no network.

Prism modules import INSIDE the test bodies so the file collects and fails at
runtime (red = rc 1) before the modules exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS_A = "workspace-a"


class _FakeTransport:
    """Records requests; returns queued canned responses keyed by (method, url
    substring)."""

    def __init__(self):
        self.calls = []
        self._responses = {}

    def stub(self, method, url_contains, response):
        self._responses[(method, url_contains)] = response

    def _match(self, method, url):
        for (m, sub), resp in self._responses.items():
            if m == method and sub in url:
                return resp
        return {}

    def post(self, url, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json or {},
                           "headers": headers or {}})
        return self._match("POST", url)

    def get(self, url, headers=None):
        self.calls.append({"method": "GET", "url": url, "json": {},
                           "headers": headers or {}})
        return self._match("GET", url)


def test_authorize_url_carries_state_and_scopes():
    from prism_service.services import jira_oauth

    url = jira_oauth.build_authorize_url("state-xyz")
    assert "state=state-xyz" in url
    assert "response_type=code" in url
    assert "scope=" in url and "offline_access" in url


def test_oauth_state_is_one_time_and_workspace_bound(tmp_path):
    from prism_service.services.jira_oauth import OAuthStateError, OAuthStateStore

    store = OAuthStateStore(str(tmp_path / "jira_oauth.db"))
    state = store.issue(WS_A)
    assert isinstance(state, str) and state

    # First consume returns the bound workspace.
    assert store.consume(state) == WS_A
    # Replay is rejected.
    with pytest.raises(OAuthStateError):
        store.consume(state)
    # An unknown state is rejected.
    with pytest.raises(OAuthStateError):
        store.consume("never-issued")


def test_cloud_id_discovery_uses_bearer_access_token():
    from prism_service.services import jira_oauth

    transport = _FakeTransport()
    transport.stub("GET", "accessible-resources",
                   [{"id": "cloud-1", "url": "https://acme.atlassian.net", "name": "acme"}])
    resources = jira_oauth.discover_cloud_id("access-abc", transport)
    assert resources[0]["id"] == "cloud-1"
    got = transport.calls[-1]
    assert got["method"] == "GET"
    assert got["headers"].get("Authorization") == "Bearer access-abc"


def test_refresh_uses_refresh_token_as_grant_not_bearer(tmp_path):
    from prism_service.services import jira_oauth
    from prism_service.services.jira_auth import JiraAuthStore

    transport = _FakeTransport()
    transport.stub("POST", "oauth/token",
                   {"access_token": "new-access", "refresh_token": "new-refresh",
                    "expires_in": 3600})

    auth = JiraAuthStore(str(tmp_path / "jira_auth.db"))
    # store an ALREADY-EXPIRED access token (expires_at in the past)
    auth.set_connection(WS_A, "cloud-1", access_token="old-access",
                        refresh_token="the-refresh-token", expires_at=1000,
                        site_url="https://acme.atlassian.net")

    def _refresh(refresh_token):
        return jira_oauth.refresh_access_token(refresh_token, transport)

    token = auth.access_token(WS_A, "cloud-1", now=2000, refresh=_refresh)
    assert token == "new-access"  # refreshed, not the stale one

    refresh_calls = [c for c in transport.calls if "oauth/token" in c["url"]]
    assert len(refresh_calls) == 1
    body = refresh_calls[0]["json"]
    assert body.get("grant_type") == "refresh_token"
    assert body.get("refresh_token") == "the-refresh-token"
    # the refresh token must NEVER be sent as a Bearer credential
    auth_header = refresh_calls[0]["headers"].get("Authorization", "")
    assert "the-refresh-token" not in auth_header

    # the new access token is persisted (a later, non-expired read returns it)
    assert auth.access_token(WS_A, "cloud-1", now=2500, refresh=_refresh) == "new-access"


def test_raw_tokens_are_never_exposed(tmp_path):
    from prism_service.services.jira_auth import JiraAuthStore

    auth = JiraAuthStore(str(tmp_path / "jira_auth.db"))
    auth.set_connection(WS_A, "cloud-1", access_token="ACCESS_SECRET_1234",
                        refresh_token="REFRESH_SECRET_9999", expires_at=9999999999)
    fp = auth.fingerprint(WS_A, "cloud-1")
    assert "ACCESS_SECRET_1234" not in fp
    assert "REFRESH_SECRET_9999" not in fp
    assert fp.endswith("1234")  # masked, last4 only
