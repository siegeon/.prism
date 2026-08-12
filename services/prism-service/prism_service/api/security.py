"""Production HTTP boundary and canonical project identity.

Team authorization belongs in front of the aggregate application, not in a
few leaf routers.  This module intentionally imports the auth/workspace
services lazily so the persistence layer can also reuse the project-id
canonicalizer without forming an import cycle.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace

from fastapi import HTTPException, Request

from prism_service.config import DEFAULT_PROJECT


def team_mode_active() -> bool:
    """True when PRISM_AUTH_MODE selects team mode (mirrors AuthService.mode,
    without constructing a service - callers are leaf response handlers that
    only need the mode, not principal resolution)."""

    return os.getenv("PRISM_AUTH_MODE", "local").strip().lower() == "team"


# Structural host-path shapes - drive-letter (C:\ or C:/) and UNC (\\host\share).
# Deliberately SHAPE-based, never a machine-specific literal (this developer's
# home directory, a specific data_dir), so a fabricated path is caught exactly
# like a real one and the check survives a change of host or account name.
_HOST_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/]"),
    re.compile(r"\\\\[^\\/]"),
)


def _looks_like_host_path(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in _HOST_PATH_PATTERNS)


def redact_host_paths(payload: dict) -> dict:
    """Drop any top-level value that structurally looks like a host
    filesystem path. Used by team-mode response handlers so a credentials
    directory, data dir, or config path never reaches a caller who is not
    the operator sitting at this machine; connection booleans and identity
    fields (which never match the path shapes above) pass through untouched.
    """

    return {k: v for k, v in payload.items() if not _looks_like_host_path(v)}


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
# What a REMOTE caller may reach in local mode before proving the key. Kept to
# the minimum the SPA needs to boot far enough to ASK for a key: which build is
# running, and which auth mode. Deliberately NOT /api/auth/my-key — that hands
# the key out, and a key you can fetch without a key is not a credential.
_REMOTE_PUBLIC_PATHS = {
    "/api/version",
    "/api/auth/mode",
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
    "/api/conductor/jobs",
}
_BODY_PROJECT_DEFAULTS = {
    "/api/brain/understand": "default",
    "/api/brain/ask": "default",
    "/api/xref/resolve_batch": "prism",
}


async def _request_project(request: Request) -> str:
    path = request.url.path.rstrip("/") or "/"
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

    # Only these handlers select a project from their JSON body.  Treating an
    # arbitrary body.project as authoritative lets a caller authorize one
    # project while a query-driven handler operates on another.  Starlette
    # caches request.json(), so the actual handler still receives the body.
    if path in _BODY_PROJECT_DEFAULTS:
        body: object = None
        content_type = request.headers.get("content-type", "").casefold()
        if "json" in content_type:
            try:
                body = await request.json()
            except (ValueError, UnicodeDecodeError):
                body = None
        default = _BODY_PROJECT_DEFAULTS[path]
        if isinstance(body, dict):
            if path == "/api/xref/resolve_batch":
                project = body.get("project") or default
            else:
                project = body.get("project", default)
        else:
            project = default
        if query_project is not None and query_project != project:
            raise ValueError("conflicting project selectors")
        return canonical_project_id(project)

    project = query_project
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
        mode = auth.mode
    except AuthenticationRequired as exc:
        raise HTTPException(401, str(exc) or "authentication required")

    path = request.url.path.rstrip("/") or "/"

    if mode != "team":
        # LOCAL MODE, REMOTE CALLER (task 4367c12f). The daemon binds 0.0.0.0,
        # so "local mode" never meant "only reachable locally" — it meant
        # "everyone is the owner", and every peer on the network got the
        # owner's data. Enforced HERE rather than per-route because only 5 of
        # 34 api modules take the current_principal dependency; tasks, brain,
        # memory and conductor do not, and those hold the data.
        if request.method == "OPTIONS" or not _protected_path(path):
            return None
        if path in _REMOTE_PUBLIC_PATHS:
            return None
        client_host = request.client.host if request.client else None
        if AuthService.is_loopback(client_host):
            return None  # the machine running PRISM is unchanged
        try:
            request.state.principal = auth.resolve_principal(
                request.headers.get("authorization"), client_host=client_host)
        except AuthenticationRequired as exc:
            raise HTTPException(
                401, str(exc) or "authentication required",
                headers={"WWW-Authenticate": "Bearer"})
        return None

    if request.method == "OPTIONS" or not _protected_path(path):
        return None
    if path in _PUBLIC_PATHS:
        return None

    try:
        principal = auth.resolve_principal(
            request.headers.get("authorization"),
            client_host=(request.client.host if request.client else None),
        )
    except AuthenticationRequired as exc:
        raise HTTPException(
            401,
            str(exc) or "authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.principal = principal

    if any(_under(path, prefix) for prefix in _DENIED_PREFIXES):
        raise HTTPException(403, "route is not workspace-scoped in team mode")
    if _under(path, "/api/github-auth"):
        if request.method in {"GET", "HEAD"} and path in {
            "/api/github-auth/status",
            "/api/github-auth/client-id",
        }:
            return principal
        raise HTTPException(403, "GitHub credentials are not workspace-scoped")
    if _under(path, "/api/update"):
        if request.method not in {"GET", "HEAD"}:
            raise HTTPException(403, "updates are disabled in team mode")
        return principal
    if any(_under(path, prefix) for prefix in _AUTH_ONLY_PREFIXES):
        return principal

    # Aggregate run/job listings become cross-workspace leaks when their
    # optional project filter is omitted.  Detail-by-id has no safe ownership
    # resolver yet, so require a selector and deny unscoped details.
    if _under(path, "/api/jobs"):
        if "project" not in request.query_params:
            raise HTTPException(403, "an explicit project is required in team mode")
    if _under(path, "/api/claude-runs"):
        if path not in {"/api/claude-runs", "/api/claude-runs/summary"}:
            raise HTTPException(403, "run detail is not workspace-scoped")
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
