"""Team principal HTTP adapter.

The domain resolver deliberately lives in ``services.auth_service`` so the
REST and MCP transports authenticate the same opaque bearer credential.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from prism_service.config import DEFAULT_PROJECT
from prism_service.models.workspace import Principal
from prism_service.api.security import query_requests_execution
from prism_service.services.auth_service import (
    AuthenticationRequired,
    AuthService,
    BootstrapSecretMismatch,
    BootstrapSecretNotConfigured,
)
from prism_service.services.workspace_service import (
    AuthorizationDenied,
    BootstrapAlreadyCompleted,
    get_workspace_service,
)


router = APIRouter()


def _auth() -> AuthService:
    return AuthService(get_workspace_service())


def _unauthorized(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=str(exc) or "authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_principal(request: Request) -> Principal:
    """Resolve the caller once; never copy credentials into request state."""
    resolved = getattr(request.state, "principal", None)
    if isinstance(resolved, Principal):
        return resolved
    try:
        resolved = _auth().resolve_principal(
            request.headers.get("authorization"),
            client_host=(request.client.host if request.client else None),
        )
        request.state.principal = resolved
        return resolved
    except AuthenticationRequired as exc:
        raise _unauthorized(exc)


def coerce_principal(value: object) -> Principal:
    """Keep direct local-mode route calls used by legacy unit tests working.

    FastAPI replaces ``Depends`` defaults on real HTTP requests.  Older tests
    call route functions directly, leaving the marker object in place; local
    mode may safely synthesize its explicit single-user principal.
    """
    if isinstance(value, Principal):
        return value
    try:
        return _auth().resolve_principal(None)
    except AuthenticationRequired as exc:
        raise _unauthorized(exc)


def require_project_role(
    principal: Principal, project: str, minimum_role: str = "viewer"
):
    if principal.mode == "local":
        return None
    try:
        return get_workspace_service().require_project_role(
            principal.user_id, project, minimum_role
        )
    except AuthorizationDenied as exc:
        raise HTTPException(403, str(exc) or "project access denied")


def require_workspace_role(
    principal: Principal, workspace_id: str, minimum_role: str = "viewer"
):
    if principal.mode == "local":
        return None
    try:
        return get_workspace_service().require_workspace_role(
            principal.user_id, workspace_id, minimum_role
        )
    except AuthorizationDenied as exc:
        raise HTTPException(403, str(exc) or "workspace access denied")


def authorize_project_request(
    request: Request,
    project: str = Query(DEFAULT_PROJECT),
) -> Principal:
    """Tasks-router guard: authenticate and authorize before ``get_project``.

    GET is viewer-safe except ``tests?run=true``, which executes code and is
    therefore a member mutation.  Every non-GET method requires member.
    """
    principal = current_principal(request)
    executes_tests = (
        request.method == "GET"
        and request.url.path.rstrip("/").endswith("/tests")
        and query_requests_execution(request)
    )
    minimum = "member" if request.method != "GET" or executes_tests else "viewer"
    require_project_role(principal, project, minimum)
    return principal


class BootstrapBody(BaseModel):
    email: str
    display_name: str = ""
    workspace_name: str


class TokenBody(BaseModel):
    label: str = ""


class ClaimBody(BaseModel):
    name: str
    email: str


@router.get("/mode")
def mode() -> dict:
    return {"mode": _auth().mode}


@router.post("/bootstrap")
def bootstrap(body: BootstrapBody, request: Request) -> dict:
    """Create the first team owner and return its bearer exactly once."""
    auth = _auth()
    if auth.mode != "team":
        raise HTTPException(409, "bootstrap is only available in team mode")
    email = (body.email or "").strip()
    name = (body.workspace_name or "").strip()
    if not email or not name:
        raise HTTPException(422, "email and workspace_name are required")
    try:
        user, workspace, issued = auth.bootstrap_owner(
            email=email,
            display_name=(body.display_name or "").strip(),
            workspace_name=name,
            supplied_secret=request.headers.get("x-prism-bootstrap-secret"),
        )
    except BootstrapSecretNotConfigured as exc:
        raise HTTPException(503, str(exc))
    except BootstrapSecretMismatch as exc:
        raise HTTPException(403, str(exc))
    except BootstrapAlreadyCompleted as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"user": user, "workspace": workspace, "token": issued.secret,
            "token_id": issued.id}


@router.get("/me")
def me(principal: Principal = Depends(current_principal)) -> dict:
    principal = coerce_principal(principal)
    service = get_workspace_service()
    user = service.get_user(principal.user_id) if principal.mode == "team" else None
    # Once the instance is CLAIMED, the owner's real name/email is on file even
    # in local mode (task fa52ba9e) — show it, not the synthetic "Local User".
    if user is None:
        owner = _auth().claimed_owner()
        if owner is not None and (
                principal.mode == "local" or principal.user_id == owner.id):
            user = owner
    memberships = (
        service.list_memberships_for_user(principal.user_id)
        if principal.mode == "team"
        else []
    )
    return {
        "mode": principal.mode,
        "user": {
            "id": principal.user_id,
            "email": user.email if user else principal.email,
            "display_name": user.display_name if user else principal.display_name,
        },
        "memberships": memberships,
    }


@router.post("/tokens")
def create_token(
    body: Optional[TokenBody] = None,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal = coerce_principal(principal)
    if principal.mode != "team":
        raise HTTPException(409, "bearer tokens are only used in team mode")
    issued = _auth().issue_token(principal.user_id, label=(body.label if body else ""))
    return {"id": issued.id, "token": issued.secret, "label": issued.label}


@router.get("/claim-status")
def claim_status() -> dict:
    """Whether the existing owner has claimed this instance yet (task
    fa52ba9e). Public: the SPA reads this before anything else to decide
    between the one-time claim screen and the normal app. An unclaimed
    instance that just auto-updated shows the claim screen."""
    return {"claimed": _auth().is_claimed()}


@router.post("/claim")
def claim(body: ClaimBody) -> dict:
    """One-time claim of a credential-free instance by its existing owner
    (name + email, no setup secret). Additive: records the owner and hands
    them their key; never touches existing tasks or memory. 409 if already
    claimed — a claimed instance can never be taken over."""
    from prism_service.services.workspace_service import InstanceAlreadyClaimed
    name = (body.name or "").strip()
    email = (body.email or "").strip()
    if not name or not email:
        raise HTTPException(422, "name and email are required")
    try:
        key = _auth().claim_instance(name=name, email=email)
    except InstanceAlreadyClaimed as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return key


@router.get("/my-key")
def my_key(principal: Principal = Depends(current_principal)) -> dict:
    """The caller's OWN access key, readable while logged in (task 6cef97ec,
    decision mx-935cc2). Scoped to the authenticated principal — you can only
    ever read your own. Mints one on first read so the owner always has a key
    to hand to agents/MCP. This is the key, not the login: identity comes from
    the instance; this is the credential you give out."""
    principal = coerce_principal(principal)
    key = _auth().my_access_key(principal.user_id)
    return {
        "id": key.get("id", ""),
        "key": key.get("secret", ""),
        "label": key.get("label", ""),
        "created_at": key.get("created_at", ""),
        "user_id": principal.user_id,
    }


@router.post("/my-key/rotate")
def rotate_my_key(principal: Principal = Depends(current_principal)) -> dict:
    """Mint a replacement key and revoke the prior one (owner: Rotate is the
    recovery path). Only rotates the caller's own key."""
    principal = coerce_principal(principal)
    key = _auth().rotate_access_key(principal.user_id)
    return {
        "id": key.get("id", ""),
        "key": key.get("secret", ""),
        "label": key.get("label", ""),
        "created_at": key.get("created_at", ""),
        "user_id": principal.user_id,
    }

