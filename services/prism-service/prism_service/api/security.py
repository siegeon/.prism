"""Production HTTP boundary and canonical project identity.

Team authorization belongs in front of the aggregate application, not in a
few leaf routers.  This module intentionally imports the auth/workspace
services lazily so the persistence layer can also reuse the project-id
canonicalizer without forming an import cycle.
"""

from __future__ import annotations

import re
from dataclasses import replace

from fastapi import HTTPException, Request

from prism_service.config import DEFAULT_PROJECT


_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.ASCII)
_WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
PYDANTIC_TRUE_STRINGS = frozenset({"1", "true", "yes", "on", "t", "y"})


def canonical_project_id(value: object) -> str:
    """Return a containment-safe project id or reject the supplied alias.

    Team-owned projects use a lowercase portable slug so one filesystem path
    can never acquire two authorization keys on case-insensitive hosts.
    Internal dots remain supported, but dot segments, trailing dots/spaces,
    separators, absolute/drive/UNC spellings, and Windows device aliases do
    not.
    """

    if not isinstance(value, str):
        raise ValueError("project id must be a string")
    if value != value.strip() or not value:
        raise ValueError("project id must be canonical")
    if not _PROJECT_ID_RE.fullmatch(value):
        raise ValueError("invalid project id")
    if value in {".", ".."} or any(part in {"", ".", ".."} for part in value.split(".")):
        raise ValueError("project id contains a dot segment")
    if value.endswith((".", " ")):
        raise ValueError("project id must not end in a dot or space")
    if value.split(".", 1)[0] in _WINDOWS_DEVICE_NAMES:
        raise ValueError("project id is a reserved filesystem name")
    return value


def query_requests_execution(request: Request) -> bool:
    """Mirror Pydantic's accepted truthy strings for the task test runner."""

    raw = request.query_params.get("run")
    return isinstance(raw, str) and raw.strip().casefold() in PYDANTIC_TRUE_STRINGS


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _protected_path(path: str) -> bool:
    return (
        _under(path, "/api")
        or _under(path, "/sse")
        or _under(path, "/graph")
        or _under(path, "/graphify-visual")
    )


_PUBLIC_PATHS = {
    "/api/version",
    "/api/auth/mode",
    "/api/auth/bootstrap",
    "/sse/live",
}
_AUTH_ONLY_PREFIXES = {
    "/api/auth",
    "/api/workspaces",
    "/api/projects",
    "/api/claude-auth",
    "/api/service-info",
    "/api/roles",
    "/api/watchdog",
}
_DENIED_PREFIXES = {
    # These surfaces use process-global state or task ids without a verified
    # project relation.  They stay unavailable in team mode until scoped.
    "/api/github-auth",
    "/api/conductor/jobs",
}


async def _request_project(request: Request) -> str:
    path = request.url.path
    if _under(path, "/graphify-visual"):
        parts = path.split("/")
        if len(parts) < 3:
            raise ValueError("project id is required")
        return canonical_project_id(parts[2])
    if _under(path, "/graph"):
        parts = path.split("/")
        if len(parts) < 4 or parts[2] != "viewer":
            raise ValueError("project id is required")
        return canonical_project_id(parts[3])

    query_values = request.query_params.getlist("project")
    if len(query_values) > 1:
        raise ValueError("project must be supplied exactly once")
    query_project = query_values[0] if query_values else None
    body_project = None

    # A few POST endpoints carry their project selector in a Pydantic body.
    # Starlette caches request.body(), so downstream body parsing sees the
    # same bytes; no receive-channel replay is required.
    if request.method not in {"GET", "HEAD"}:
        content_type = request.headers.get("content-type", "").casefold()
        if "json" in content_type:
            try:
                body = await request.json()
            except (ValueError, UnicodeDecodeError):
                body = None
            if isinstance(body, dict) and body.get("project") is not None:
                body_project = body["project"]
    if (
        query_project is not None
        and body_project is not None
        and query_project != body_project
    ):
        raise ValueError("conflicting project selectors")
    project = body_project if body_project is not None else query_project
    if project is None and _under(path, "/api/xref"):
        project = "prism"  # xref's documented legacy default
    return canonical_project_id(DEFAULT_PROJECT if project is None else project)


async def enforce_team_boundary(request: Request):
    """Authenticate and authorize every production data/control route.

    Local mode exits before classification, preserving the existing
    single-user application.  Team mode has a deliberately tiny public
    allowlist and a fail-closed default: unclassified protected routes are
    treated as project data rather than silently becoming public.
    """

    from prism_service.services.auth_service import (
        AuthenticationRequired,
        AuthService,
    )
    from prism_service.services.workspace_service import (
        AuthorizationDenied,
        get_workspace_service,
    )

    auth = AuthService(get_workspace_service())
    try:
        if auth.mode != "team":
            return None
    except AuthenticationRequired as exc:
        raise HTTPException(401, str(exc) or "authentication required")

    path = request.url.path.rstrip("/") or "/"
    if request.method == "OPTIONS" or not _protected_path(path):
        return None
    if path in _PUBLIC_PATHS:
        return None

    try:
        principal = auth.resolve_principal(request.headers.get("authorization"))
    except AuthenticationRequired as exc:
        raise HTTPException(
            401,
            str(exc) or "authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.principal = principal

    if any(_under(path, prefix) for prefix in _DENIED_PREFIXES):
        raise HTTPException(403, "route is not workspace-scoped in team mode")
    if _under(path, "/api/update"):
        if request.method not in {"GET", "HEAD"}:
            raise HTTPException(403, "updates are disabled in team mode")
        return principal
    if any(_under(path, prefix) for prefix in _AUTH_ONLY_PREFIXES):
        return principal

    # Aggregate run/job listings become cross-workspace leaks when their
    # optional project filter is omitted.  Detail-by-id has no safe ownership
    # resolver yet, so require a selector and deny unscoped details.
    if _under(path, "/api/jobs") or _under(path, "/api/claude-runs"):
        if "project" not in request.query_params:
            raise HTTPException(403, "an explicit project is required in team mode")
    if path == "/api/conductor/flow/workspace":
        raise HTTPException(403, "task workspace lookup is not project-scoped")

    try:
        project_id = await _request_project(request)
        minimum_role = "viewer"
        if request.method not in {"GET", "HEAD"}:
            minimum_role = "member"
        elif path.endswith("/tests") and query_requests_execution(request):
            minimum_role = "member"
        membership = get_workspace_service().require_project_role(
            principal.user_id, project_id, minimum_role
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc) or "invalid project id")
    except AuthorizationDenied as exc:
        raise HTTPException(403, str(exc) or "project access denied")

    principal = replace(principal, role=membership.role)
    request.state.principal = principal
    request.state.project_id = project_id
    return principal
