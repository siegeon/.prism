"""RED — Jira API-token connect (task 64ba4755).

Owner reversal of mx-639efa on 2026-08-10: Jira gets a Basic-auth API-token
connect path (no OAuth app required on this instance), alongside the existing
OAuth route. Pins:

  FR-1: POST .../jira/api-token validates via GET <site>/rest/api/3/myself
        with Basic auth, resolves a cloud id best-effort, persists through
        JiraAuthStore, and creates an integration connection row so the
        connector reports connected.
  FR-2: jira_connections gains auth_kind/account_email/account_name
        additively; an existing OAuth row (no auth_kind written) still reads
        'oauth' and keeps the needs_attention behaviour
        test_connectors_section.py pins.
  FR-3: JiraClient gets a Basic-auth mode against https://<site>/rest/api/3.
  FR-4: POST .../jira/track accepts a bare project key and attaches to the
        connection the connect step already created — never mints a second,
        credential-less one; github's owner/repo validation is untouched.
  NFR:  the raw API token never appears in a response body or a store dump.
"""

from __future__ import annotations

import base64
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

SITE = "https://acme.atlassian.net"
SCOPE = "personal-local-user"


class _FakeConnectTransport:
    """Fake for the connect front door's injectable `_transports` seam
    (api/integrations_connect.py:44-51) — never a stand-in for JiraClient."""

    def __init__(self, myself=None, tenant_info=None, raise_on_myself=None):
        self.myself = myself if myself is not None else {}
        self.tenant_info = tenant_info
        self.raise_on_myself = raise_on_myself
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        if "/myself" in url:
            if self.raise_on_myself is not None:
                raise self.raise_on_myself
            return self.myself
        if "_edge/tenant_info" in url:
            if self.tenant_info is None:
                raise RuntimeError("tenant_info unavailable")
            return self.tenant_info
        return {}


@pytest.fixture
def app(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PRISM_JIRA_CLIENT_ID", raising=False)

    from prism_service.api import integrations_connect as connect
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

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        yield client
    set_integration_store(None)
    set_jira_auth_store(None)
    set_workspace_service(None)


def _connect_transport(app, transport):
    from prism_service.api import integrations_connect as connect

    connect.set_transport("jira", transport)


# ── FR-1: connect via site + email + token flips status to connected ──────

def test_api_token_connect_validates_via_myself_and_persists(app):
    myself = {"accountId": "acc-1", "displayName": "Ada Lovelace",
              "emailAddress": "ada@acme.com"}
    transport = _FakeConnectTransport(
        myself=myself, tenant_info={"cloudId": "cloud-xyz"})
    _connect_transport(app, transport)

    resp = app.post("/api/integrations/connect/jira/api-token", json={
        "site_url": SITE, "email": "ada@acme.com", "api_token": "secret-tok-1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is True
    assert "Ada Lovelace" in body.get("account", "")

    # myself was actually called with Basic auth of email:token
    myself_calls = [c for c in transport.calls if "/myself" in c[0]]
    assert myself_calls, "the connect endpoint never called /rest/api/3/myself"
    url, headers = myself_calls[0]
    assert url.startswith(f"{SITE}/rest/api/3/myself")
    expected = "Basic " + base64.b64encode(b"ada@acme.com:secret-tok-1").decode()
    assert headers.get("Authorization") == expected

    # the status endpoint now reports Jira connected with no client id set
    rows = {r["provider"]: r for r in
            app.get("/api/integrations/connect/status").json()["connectors"]}
    assert rows["jira"]["state"] == "connected", rows["jira"]
    assert "Ada Lovelace" in rows["jira"]["account"]


def test_api_token_connect_rejects_a_bad_credential(app):
    transport = _FakeConnectTransport(raise_on_myself=RuntimeError("401 unauthorized"))
    _connect_transport(app, transport)

    resp = app.post("/api/integrations/connect/jira/api-token", json={
        "site_url": SITE, "email": "ada@acme.com", "api_token": "wrong-token"})
    assert resp.status_code in (400, 401, 422), resp.text
    # no connection was persisted on a rejected credential
    rows = {r["provider"]: r for r in
            app.get("/api/integrations/connect/status").json()["connectors"]}
    assert rows["jira"]["state"] != "connected"


def test_api_token_connect_falls_back_when_tenant_info_is_unsupported(app):
    """tenant_info is explicitly NOT a supported Atlassian API (plan research
    rung) — a failure there must never fail the whole connect."""
    myself = {"accountId": "acc-2", "displayName": "Bob", "emailAddress": "bob@acme.com"}
    transport = _FakeConnectTransport(myself=myself, tenant_info=None)
    _connect_transport(app, transport)

    resp = app.post("/api/integrations/connect/jira/api-token", json={
        "site_url": SITE, "email": "bob@acme.com", "api_token": "tok-2"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["connected"] is True


# ── NFR: the raw token never crosses into a response, log, or store dump ──

def test_raw_token_never_appears_in_response_or_store(app, tmp_path):
    secret = "SUPER_SECRET_ATLASSIAN_TOKEN_998877"
    myself = {"accountId": "acc-3", "displayName": "Cat", "emailAddress": "cat@acme.com"}
    transport = _FakeConnectTransport(myself=myself, tenant_info={"cloudId": "cloud-cat"})
    _connect_transport(app, transport)

    resp = app.post("/api/integrations/connect/jira/api-token", json={
        "site_url": SITE, "email": "cat@acme.com", "api_token": secret})
    assert secret not in resp.text

    status = app.get("/api/integrations/connect/status")
    assert secret not in status.text

    raw = sqlite3.connect(str(tmp_path / "data" / "jira_auth.db")
                          if (tmp_path / "data" / "jira_auth.db").exists()
                          else str(tmp_path / "jira_auth.db"))
    try:
        dump = "\n".join(raw.iterdump())
    finally:
        raw.close()
    assert secret not in dump


# ── FR-2: schema is additive; a pre-existing OAuth row is unaffected ──────

def test_existing_oauth_row_defaults_auth_kind_and_still_needs_attention(tmp_path):
    """An OAuth row written by TODAY's schema (no auth_kind column at all)
    must upgrade in place and keep reading as 'oauth' — the exact row
    test_connectors_section.py's expired-credential case depends on."""
    from prism_service.services.jira_auth import JiraAuthStore

    db_path = str(tmp_path / "jira_auth.db")
    # Write a row through TODAY's schema shape, before any migration.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE jira_connections (workspace_id TEXT NOT NULL, "
        "cloud_id TEXT NOT NULL, access_token TEXT NOT NULL DEFAULT '', "
        "refresh_token TEXT NOT NULL DEFAULT '', expires_at INTEGER NOT NULL "
        "DEFAULT 0, site_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT "
        "NULL, updated_at TEXT NOT NULL, PRIMARY KEY (workspace_id, cloud_id))")
    conn.execute(
        "INSERT INTO jira_connections VALUES (?,?,?,?,?,?,?,?)",
        (SCOPE, "cloud-legacy", "old-access", "old-refresh", 1, SITE,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()

    store = JiraAuthStore(db_path)  # must migrate additively, not recreate
    assert store.auth_kind(SCOPE, "cloud-legacy") == "oauth", (
        "a pre-existing OAuth row must default to auth_kind='oauth'")

    def _refuse_refresh(_rt):
        raise RuntimeError("refresh unavailable")

    with pytest.raises(Exception):
        store.access_token(SCOPE, "cloud-legacy", now=999999999, refresh=_refuse_refresh)


def test_api_token_row_never_refreshes(tmp_path):
    """An api_token row has no refresh grant — access_token() must return the
    stored secret directly rather than looping into the OAuth refresh path
    (plan S2), which would read this healthy credential as needs_attention
    forever."""
    from prism_service.services.jira_auth import JiraAuthStore

    store = JiraAuthStore(str(tmp_path / "jira_auth.db"))
    store.set_connection(
        SCOPE, "cloud-tok", access_token="tok-value", refresh_token="",
        expires_at=0, site_url=SITE, auth_kind="api_token",
        account_email="ada@acme.com")

    def _boom(_rt):
        raise AssertionError("api_token rows must never call refresh()")

    # `now` is far in the future — an oauth row would be expired here.
    got = store.access_token(SCOPE, "cloud-tok", now=99999999999, refresh=_boom)
    assert got == "tok-value"


# ── FR-3: JiraClient Basic-auth mode against https://<site>/rest/api/3 ────

def test_jira_client_basic_mode_targets_the_site_not_the_cloud_gateway():
    from prism_service.services.jira_auth import JiraCredential
    from prism_service.services.jira_client import JiraClient

    class _Fake:
        def __init__(self):
            self.calls = []

        def get(self, url, headers):
            self.calls.append((url, headers))
            return {"issues": [], "nextPageToken": None}

    fake = _Fake()
    client = JiraClient(transport=fake)
    cred = JiraCredential(kind="api_token", secret="tok-abc",
                          email="ada@acme.com", site_url=SITE, cloud_id="")

    client.search_jql("unused-cloud-id", cred, 'project = "PROJ"')

    assert fake.calls, "JiraClient never called the transport"
    url, headers = fake.calls[0]
    assert url.startswith(f"{SITE}/rest/api/3/search/jql"), url
    assert "api.atlassian.com" not in url
    expected = "Basic " + base64.b64encode(b"ada@acme.com:tok-abc").decode()
    assert headers.get("Authorization") == expected


def test_jira_client_bearer_mode_is_unchanged_for_a_plain_string_token():
    """Back-compat: today's callers pass a bare token string (plan S4) — the
    OAuth Bearer path against api.atlassian.com must be untouched."""
    from prism_service.services.jira_client import JiraClient

    class _Fake:
        def __init__(self):
            self.calls = []

        def get(self, url, headers):
            self.calls.append((url, headers))
            return {"issues": [], "nextPageToken": None}

    fake = _Fake()
    client = JiraClient(transport=fake)
    client.search_jql("cloud-1", "bearer-secret", 'project = "PROJ"')

    url, headers = fake.calls[0]
    assert url.startswith("https://api.atlassian.com/ex/jira/cloud-1/rest/api/3")
    assert headers.get("Authorization") == "Bearer bearer-secret"


# ── FR-4: track accepts a bare Jira project key, attaches, never mints ────

def test_track_jira_bare_project_key_attaches_to_the_connect_step(app):
    from prism_service.services.integration_store import get_integration_store

    myself = {"accountId": "acc-4", "displayName": "Dee", "emailAddress": "dee@acme.com"}
    transport = _FakeConnectTransport(myself=myself, tenant_info={"cloudId": "cloud-dee"})
    _connect_transport(app, transport)
    connect_resp = app.post("/api/integrations/connect/jira/api-token", json={
        "site_url": SITE, "email": "dee@acme.com", "api_token": "tok-dee"})
    assert connect_resp.status_code == 200, connect_resp.text

    before = len(get_integration_store().list_connections(SCOPE, "jira"))

    resp = app.post("/api/integrations/connect/jira/track", json={"repo": "PROJ"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["repo"] == "PROJ"

    after = get_integration_store().list_connections(SCOPE, "jira")
    assert len(after) == before, (
        "tracking a project key must attach to the connection connect already "
        "made, never mint a second, credential-less one")
    assert body["connection_id"] == after[0].id


def test_track_jira_without_a_connection_is_a_clean_409(app):
    resp = app.post("/api/integrations/connect/jira/track", json={"repo": "PROJ"})
    assert resp.status_code == 409, resp.text


def test_track_github_owner_repo_validation_is_untouched(app):
    """FR-4's jira branch must not weaken github's existing 422 guard."""
    resp = app.post("/api/integrations/connect/github/track", json={"repo": "PROJ"})
    assert resp.status_code == 422, (
        "github tracking must still require owner/repo; jira's bare-key "
        f"branch leaked into github's validation: {resp.text}")


# ── Production transport default (owner's live 503, 2026-08-10) ───────────
# The owner's FIRST real connect attempt returned
#   503 {"detail":"jira transport is not configured"}
# because every test in this file INJECTS the transport (set_transport) and
# no production code ever constructed one — mx-8f4666's built-but-unwired
# shape, at the seam itself. The seam must default to a REAL transport when
# nothing is injected; an injected transport still wins.

def test_uninjected_jira_transport_is_real_never_503():
    from prism_service.api import integrations_connect as ic

    snapshot = dict(ic._transports)
    try:
        ic.reset_transports()
        transport = ic._transport("jira")
        assert callable(getattr(transport, "get", None)), (
            "with nothing injected, _transport('jira') must hand back a real "
            "transport (the owner's live connect 503'd here), not raise")
        assert callable(getattr(transport, "post", None)), (
            "the default transport must also speak POST — jira_oauth's "
            "exchange/refresh path posts through this same seam")
    finally:
        ic.reset_transports()
        ic._transports.update(snapshot)


def test_injected_transport_still_wins_over_the_default():
    from prism_service.api import integrations_connect as ic

    snapshot = dict(ic._transports)
    sentinel = object()
    try:
        ic.set_transport("jira", sentinel)
        assert ic._transport("jira") is sentinel, (
            "tests drive the round trip offline via set_transport; the "
            "production default must never shadow an injected transport")
    finally:
        ic.reset_transports()
        ic._transports.update(snapshot)
