"""Jira / Atlassian OAuth 3LO — the primary connect path.

The operator clicks `Connect Jira`; PRISM sends the browser to Atlassian's
`/authorize` endpoint, Atlassian redirects back to `/api/jira/callback`
with a code, and exchange_code() trades it for a refresh token that
jira_auth.set_oauth_token() persists. Stdlib-only (no httpx dep),
mirroring github_oauth.py. The token never crosses the API.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"

# Scopes PRISM needs to read/track issues; offline_access mints the
# refresh token we persist.
DEFAULT_SCOPES = "read:jira-work read:jira-user write:jira-work offline_access"


def client_id() -> str:
    return os.environ.get("PRISM_JIRA_CLIENT_ID", "").strip() or "prism-jira-oauth-app"


def client_secret() -> str:
    return os.environ.get("PRISM_JIRA_CLIENT_SECRET", "").strip()


def redirect_uri() -> str:
    env = os.environ.get("PRISM_JIRA_REDIRECT_URI", "").strip()
    if env:
        return env
    port = os.environ.get("PRISM_UI_PORT", "7778")
    return f"http://localhost:{port}/api/jira/callback"


def new_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(state: str, scopes: str = DEFAULT_SCOPES) -> str:
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


def exchange_code(code: str, state: str = "") -> dict:
    """Trade an authorization code for an access + refresh token pair.

    Posts to Atlassian's token endpoint. Raises RuntimeError on failure;
    the callback route catches it and redirects to an error landing."""
    body = json.dumps({
        "grant_type": "authorization_code",
        "client_id": client_id(),
        "client_secret": client_secret(),
        "code": code,
        "redirect_uri": redirect_uri(),
    }).encode("ascii")
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "prism-service"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"atlassian http {e.code}: {raw[:200]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"atlassian network error: {e.reason}") from e
