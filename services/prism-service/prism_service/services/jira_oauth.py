"""Atlassian OAuth 3LO — authorize state, token exchange, cloudId, refresh.

Corrects the stale feat/connect-jira-oauth branch: authorize state is now a
DURABLE ONE-TIME value bound to the initiating workspace (replay/unknown are
rejected), and the refresh path uses the refresh token strictly as the
``grant_type=refresh_token`` request body — it is never sent as a Bearer
credential. All network I/O goes through an injected ``transport`` (``.post``/
``.get`` returning parsed JSON) so the lifecycle is deterministically testable.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

from prism_service.services import sqlite_db

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
DEFAULT_SCOPES = "read:jira-work read:jira-user offline_access"


class OAuthStateError(RuntimeError):
    """Raised for an unknown or already-consumed (replayed) OAuth state."""


def client_id() -> str:
    return os.environ.get("PRISM_JIRA_CLIENT_ID", "").strip() or "prism-jira-oauth-app"


def client_secret() -> str:
    return os.environ.get("PRISM_JIRA_CLIENT_SECRET", "").strip()


def redirect_uri() -> str:
    return os.environ.get("PRISM_JIRA_REDIRECT_URI", "").strip() or \
        "http://localhost:8888/api/jira/callback"


def build_authorize_url(state: str, scopes: str = DEFAULT_SCOPES) -> str:
    import urllib.parse

    params = {
        "audience": "api.atlassian.com",
        "client_id": client_id(),
        "scope": scopes,
        "redirect_uri": redirect_uri(),
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class OAuthStateStore:
    """Durable, one-time authorize states bound to a workspace."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS jira_oauth_states (
        state TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)
        self._tlocal = threading.local()
        self._db.executescript(self._SCHEMA)

    @property
    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = sqlite_db.connect(self._db_path, timeout=5.0)
            self._tlocal.conn = conn
        return conn

    def issue(self, workspace_id: str) -> str:
        state = secrets.token_urlsafe(24)
        with self._db:
            self._db.execute(
                "INSERT INTO jira_oauth_states (state, workspace_id, created_at) "
                "VALUES (?, ?, ?)",
                (state, workspace_id, datetime.now(timezone.utc).isoformat()),
            )
        return state

    def consume(self, state: str) -> str:
        """Return the bound workspace and delete the state. A replayed or
        unknown state raises — the row is gone after the first successful use."""
        with self._db:
            row = self._db.execute(
                "SELECT workspace_id FROM jira_oauth_states WHERE state = ?",
                (state,),
            ).fetchone()
            if row is None:
                raise OAuthStateError("unknown or already-consumed oauth state")
            self._db.execute("DELETE FROM jira_oauth_states WHERE state = ?", (state,))
        return row["workspace_id"]


def exchange_code(code: str, transport, redirect_uri_override: str = "") -> dict:
    """Trade an authorization code for an access + refresh token pair."""
    return transport.post(TOKEN_URL, json={
        "grant_type": "authorization_code",
        "client_id": client_id(),
        "client_secret": client_secret(),
        "code": code,
        "redirect_uri": redirect_uri_override or redirect_uri(),
    })


def refresh_access_token(refresh_token: str, transport) -> dict:
    """Exchange a refresh token for a fresh access token.

    The refresh token rides in the request BODY as the ``refresh_token`` grant
    — it is NEVER placed in an Authorization header. No Bearer here.
    """
    return transport.post(TOKEN_URL, json={
        "grant_type": "refresh_token",
        "client_id": client_id(),
        "client_secret": client_secret(),
        "refresh_token": refresh_token,
    })


def discover_cloud_id(access_token: str, transport) -> list[dict]:
    """List the Atlassian sites this access token can reach (accessible
    resources). The ACCESS token is the Bearer here — this is the one call the
    access token authorizes directly."""
    return transport.get(
        ACCESSIBLE_RESOURCES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
