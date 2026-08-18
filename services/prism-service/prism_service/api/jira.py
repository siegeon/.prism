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
from prism_service.services import jira_mappings
from prism_service.services import jira_oauth
from prism_service.services import jira_sync

router = APIRouter()


class MappingBody(BaseModel):
    """Bind a PRISM project to a Jira project key."""
    project_id: str = ""
    jira_project_key: str = ""

_SPA_CONNECTIONS = "/settings/integrations"


class PullBody(BaseModel):
    """Deterministic pull-in — optionally override the Jira project key
    (else it resolves from the project's mapping / PRISM_JIRA_PROJECT_KEY)."""
    project_key: str = ""


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


def _pull_task_svc(project: str):
    """The task service to pull Jira issues into — the current project's
    store. Broken out so tests can route it at an isolated TaskService."""
    from prism_service.project_context import get_project
    return get_project(project).task_svc


@router.post("/pull")
def pull(body: PullBody, project: str = "default") -> dict:
    """Deterministically PULL a Jira project's issues in as PRISM tasks
    (upsert by jira_issue_key — no inference). Returns {created, updated,
    pulled, receipts}; empty-safe {pulled:0, reason} when disconnected or
    no project resolves."""
    svc = _pull_task_svc(project)
    key = (body.project_key or "").strip() or None
    return jira_sync.pull_from_jira(svc, jira_project_key=key)


@router.get("/projects")
def list_jira_projects() -> dict:
    """The real Jira projects the credential can see ([{key, name}]) — the
    Integrations UI populates its mapping dropdown from this and auto-suggests
    a PRISM-project ⇌ Jira-project pairing by name/key affinity. Empty-safe
    ({projects: []}) when disconnected or the call fails, so the UI degrades
    to the plain form rather than erroring."""
    from prism_service.services import jira_client
    if not ja.is_authenticated():
        return {"projects": []}
    try:
        return {"projects": jira_client.list_projects()}
    except Exception:
        return {"projects": []}


@router.get("/mappings")
def list_mappings() -> dict:
    """Current PRISM-project -> Jira-project-key bindings (no secrets)."""
    return {"mappings": jira_mappings.default_service().list()}


@router.post("/mapping")
def set_mapping(body: MappingBody) -> dict:
    """Bind (or rebind) a PRISM project to a Jira project key."""
    pid = (body.project_id or "").strip()
    key = (body.jira_project_key or "").strip()
    if not pid or not key:
        raise HTTPException(400, "project_id and jira_project_key are required")
    row = jira_mappings.default_service().set(pid, key)
    return {"mapping": row, "mappings": jira_mappings.default_service().list()}


@router.delete("/mapping")
def delete_mapping(project_id: str = "") -> dict:
    """Remove a PRISM project's Jira binding. Idempotent."""
    pid = (project_id or "").strip()
    if not pid:
        raise HTTPException(400, "project_id is required")
    deleted = jira_mappings.default_service().delete(pid)
    return {"deleted": deleted, "mappings": jira_mappings.default_service().list()}


@router.post("/connect")
def connect(body: ConnectBody) -> dict:
    token = (body.refresh_token or body.access_token or "").strip()
    if not token:
        raise HTTPException(400, "an access_token or refresh_token is required")
    ja.set_oauth_token(token, email=body.email, base_url=body.base_url)
    return _status_payload()
