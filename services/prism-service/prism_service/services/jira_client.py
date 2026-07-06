"""Stdlib-urllib Jira REST client — the network seam Slice D syncs over.

Module-level functions (create_issue / get_issue / list_updated_since) so
tests can MONKEYPATCH them and never touch real Jira. Credentials come from
jira_auth's server-side store; the raw token is used to sign the request and
is NEVER printed, returned, or folded into an error string. No third-party
deps (mirrors jira_oauth.py) — urllib.request only.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from prism_service.services import jira_auth

_TIMEOUT_S = 15


class JiraClientError(RuntimeError):
    """A Jira request failed. Message is sanitized — never carries a token."""


def _cred() -> dict:
    """The active server-side credential, or raise if Jira isn't connected.
    Uses jira_auth's internal accessor (same package) — the token stays in
    the process, exactly as jira_auth guarantees."""
    cred = jira_auth._effective()
    if not cred:
        raise JiraClientError("jira not connected")
    return cred


def _api_base(cred: dict) -> str:
    base = (cred.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise JiraClientError("no jira base_url configured")
    return f"{base}/rest/api/3"


def _auth_header(cred: dict) -> str:
    """Basic (email:api_token) or Bearer (oauth) — the only place the raw
    token is touched. Callers never see it."""
    token = cred.get("token") or ""
    if cred.get("auth_type") == "oauth":
        return f"Bearer {token}"
    raw = f"{cred.get('email', '')}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _request(method: str, url: str, payload: dict | None = None) -> dict:
    """One authenticated JSON round-trip. Raises JiraClientError on any
    non-2xx / network error — with a message that never carries the token."""
    cred = _cred()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": _auth_header(cred),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "prism-service",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            body = r.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        # e.read() may echo the request; status + short reason only, no token.
        raise JiraClientError(f"jira http {e.code}") from None
    except urllib.error.URLError as e:
        raise JiraClientError(f"jira network error: {e.reason}") from None


def _jql_time(ts) -> str:
    """A JQL-friendly 'yyyy-MM-dd HH:mm' from an epoch-seconds int/float, or
    pass a string through unchanged (already-formatted JQL time)."""
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(ts)


# --------------------------------------------------------------------------
# Public, MONKEYPATCHABLE surface. jira_sync calls these by module attribute
# so a test can replace any of them with an in-memory fake.
# --------------------------------------------------------------------------

def create_issue(project_key: str, summary: str) -> str:
    """Create a Jira issue and return its key (e.g. 'PLAT-1')."""
    cred = _cred()
    url = f"{_api_base(cred)}/issue"
    data = _request("POST", url, {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": "Task"},
        },
    })
    return str(data.get("key") or "")


def get_issue(key: str) -> dict:
    """Fetch a single issue (returns the parsed {id, key, fields, ...})."""
    cred = _cred()
    return _request("GET", f"{_api_base(cred)}/issue/{urllib.parse.quote(key)}")


def list_updated_since(ts) -> list[dict]:
    """Issues updated at/after `ts` (epoch seconds or a JQL time string),
    oldest-first. Returns the raw issue dicts ([] when none)."""
    cred = _cred()
    jql = f'updated >= "{_jql_time(ts)}" ORDER BY updated ASC'
    q = urllib.parse.urlencode({"jql": jql, "maxResults": 50})
    data = _request("GET", f"{_api_base(cred)}/search?{q}")
    issues = data.get("issues")
    return issues if isinstance(issues, list) else []
