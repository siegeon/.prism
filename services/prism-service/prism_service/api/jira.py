"""/api/jira — connect PRISM to Jira via OAuth 3LO (primary) or an
email + API-token basic-auth fallback.

Both paths land in jira_auth's chmod-600 store; only a masked fingerprint
(`oauth:•••last4` / `<email>:•••last4`) ever crosses this API. Mirrors
github_auth.py's status/configure/clear shape.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from prism_service.services import jira_auth as ja
from prism_service.services import jira_oauth
from prism_service.services import jira_sync

router = APIRouter()

_SPA_CONNECTIONS = "/settings/connections"


class ConfigureBody(BaseModel):
    """email + API-token fallback. Extra fields (token/refresh_token/…) ignored."""
    email: str = ""
    api_token: str = ""
    base_url: str = ""


class ConnectBody(BaseModel):
    """OAuth token landing — access or refresh token from the 3LO dance."""
    access_token: str = ""
    refresh_token: str = ""
    email: str = ""
    base_url: str = ""


def _status_payload() -> dict:
    authed = ja.is_authenticated()
    meta = ja.current_meta()
    return {
        "authenticated": authed,
        "credentials_path": str(ja.credentials_path()),
        "fingerprint": ja.token_fingerprint() if authed else "",
        "from_env": ja.is_from_env(),
        "source": ja.source(),
        "auth_type": meta.get("auth_type", ""),
        "email": meta.get("email", ""),
        "base_url": meta.get("base_url", ""),
    }


@router.get("/status")
def status() -> dict:
    return _status_payload()


@router.post("/clear")
def clear() -> dict:
    ja.clear_token()
    return _status_payload()


def _authorize_payload() -> dict:
    state = jira_oauth.new_state()
    return {"authorize_url": jira_oauth.build_authorize_url(state), "state": state}


@router.get("/authorize")
def authorize() -> dict:
    return _authorize_payload()


@router.post("/authorize")
def authorize_post() -> dict:
    return _authorize_payload()


@router.get("/callback")
def callback(code: str = "", state: str = ""):
    if not code:
        return RedirectResponse(f"{_SPA_CONNECTIONS}?jira=error", status_code=303)
    try:
        tokens = jira_oauth.exchange_code(code, state=state)
    except Exception:
        return RedirectResponse(f"{_SPA_CONNECTIONS}?jira=error", status_code=303)
    token = (tokens.get("refresh_token") or tokens.get("access_token") or "").strip()
    if token:
        ja.set_oauth_token(token)
    return RedirectResponse(f"{_SPA_CONNECTIONS}?jira=connected", status_code=303)


@router.post("/configure")
def configure(body: ConfigureBody) -> dict:
    try:
        ja.set_basic_credentials(body.email, body.api_token, base_url=body.base_url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _status_payload()


@router.get("/sync/receipts")
def sync_receipts(limit: int = 50) -> dict:
    """Recent bidirectional-sync receipts for the UI. No secrets — only
    {direction, task_id, jira_issue_key, ok, ts}."""
    return {"receipts": jira_sync.recent_receipts(limit=limit)}


@router.post("/connect")
def connect(body: ConnectBody) -> dict:
    token = (body.refresh_token or body.access_token or "").strip()
    if not token:
        raise HTTPException(400, "an access_token or refresh_token is required")
    ja.set_oauth_token(token, email=body.email, base_url=body.base_url)
    return _status_payload()
