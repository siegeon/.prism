"""PRISM MCP server — StreamableHTTP transport with per-project scoping.

The project is determined by the ?project= query parameter on the request URL.
E.g.  http://localhost:7777/mcp?project=my-app

If omitted, the "default" project is used.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from urllib.parse import parse_qs

import uvicorn
from mcp.types import CallToolResult, TextContent
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from prism_service.config import DEFAULT_PROJECT, PROJECTS_DIR
from prism_service.api.security import canonical_project_id
from prism_service.mcp.instructions import PRISM_INSTRUCTIONS
from prism_service.mcp.request_context import (
    PrismRequestContext,
    get_request_context,
    use_request_context,
)
from prism_service.mcp.tools import (
    TOOLS,
    handle_tool,
    tool_names_for_profile,
    tools_for_profile,
)
from prism_service.services.auth_service import AuthenticationRequired, AuthService
from prism_service.models.workspace import role_allows
from prism_service.services.workspace_service import (
    AuthorizationDenied,
    ProjectOwnershipConflict,
    get_workspace_service,
)

# ---------------------------------------------------------------------------
# MCP Server instance
# ---------------------------------------------------------------------------

server = Server("prism-service", instructions=PRISM_INSTRUCTIONS)


# Fail closed for new tools: a viewer may use only this explicit read surface;
# everything else requires member. Project creation is separately admin-only.
_MCP_VIEWER_TOOLS = {
    "project_list",
    "task_list",
    "task_next",
    "workflow_state",
    "context_bundle",
    "brain_search",
    "brain_find_symbol",
    "brain_find_references",
    "brain_call_chain",
    "brain_outline",
    "brain_understand",
    "memory_recall",
    "prism_guide",
}
# Team mode: tools whose schema takes an UNSCOPED host filesystem path
# (register_claude_source.cwd, verifier_run.workspace) can dereference a path
# outside any workspace, so they FAIL CLOSED in team mode until ownership can
# constrain the path. Local single-user mode keeps them available. This set is
# pinned to the schema scan by test_team_boundary_hardening::
# test_mcp_host_path_schemas_are_explicitly_classified_for_team_mode — a new
# host-path tool must be classified here (or its schema field removed) or that
# test fails, so the deny set can never silently drift behind the tools.
_TEAM_UNSCOPED_HOST_PATH_TOOLS = frozenset({"register_claude_source", "verifier_run"})


def _tool_error(status: int, detail: str) -> CallToolResult:
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps({
            "error": detail,
            "status": status,
        }))],
    )


def _authorize_tool(name: str, ctx: PrismRequestContext):
    """Return the caller's membership or a structured MCP error."""
    if ctx.principal.mode == "local":
        return None
    # Discovery and creation are identity/workspace scoped.  Requiring a
    # current project here would deadlock an empty workspace before either
    # tool could run.
    if name in {"project_list", "project_create"}:
        return None
    minimum = (
        "viewer" if name in _MCP_VIEWER_TOOLS
        else "member"
    )
    try:
        project_id = canonical_project_id(ctx.project_id)
        return get_workspace_service().require_project_role(
            ctx.principal.user_id, project_id, minimum
        )
    except ValueError:
        return _tool_error(400, "invalid project id")
    except AuthorizationDenied:
        return _tool_error(403, "project access denied")


@server.list_tools()
async def list_tools():
    """Return tools enabled for the current request profile."""
    return tools_for_profile(get_request_context().tool_profile)


def _record_tool_call(name: str, ctx, result) -> None:
    """Count one dispatched call for the tool-usage ledger (task f1e7e228).

    Wrapped in a blanket try/except ON PURPOSE: telemetry must never be able
    to fail a tool call. A write that cannot land is simply no evidence.
    """
    try:
        from prism_service.project_context import get_project
        from prism_service.services.tool_usage_data import record_tool_call

        record_tool_call(
            str(get_project(ctx.project_id)._data_dir / "scores.db"),
            tool=name,
            project=ctx.project_id,
            tool_profile=ctx.tool_profile or "interactive",
            ok=not bool(getattr(result, "isError", False)),
        )
    except Exception:
        pass


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch a tool call to the handler, scoped to the current project,
    and record per-tool telemetry for EVERY outcome — including the
    profile-rejected ones, which are the ledger's most interesting signal."""
    ctx = get_request_context()
    result = await _dispatch_tool(name, arguments, ctx)
    _record_tool_call(name, ctx, result)
    return result


async def _dispatch_tool(name: str, arguments: dict, ctx):
    """The dispatch body: authorize, profile-check, team-scope, hand off."""
    authorized = _authorize_tool(name, ctx)
    if isinstance(authorized, CallToolResult):
        return authorized
    if name not in tool_names_for_profile(ctx.tool_profile):
        # GH #99 part 3: a rejected (or unknown) tool must surface as
        # CallToolResult.isError=True, not a bare TextContent list that
        # the SDK stamps isError=False (a false-success that silently
        # drops telemetry writes). Return the structured CallToolResult
        # directly so the helpful payload survives (raising would lose it
        # to str(e)).
        known = name in {tool.name for tool in TOOLS}
        if known:
            error = "Tool is not available for this MCP tool profile."
            hint = "Reconnect with ?tool_profile=all for maintenance-only tools."
        else:
            error = "Unknown tool — not registered on this MCP server."
            hint = "Check the tool name; reconnect with ?tool_profile=all to list all tools."
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=json.dumps({
                "error": error,
                "tool": name,
                "tool_profile": ctx.tool_profile,
                "hint": hint,
            }, indent=2))],
        )
    if ctx.principal.mode == "team" and name not in {
        "project_list",
        "project_create",
    }:
        explicit_project = (arguments or {}).get("project")
        if explicit_project:
            try:
                target_project = canonical_project_id(explicit_project)
                selected_project = canonical_project_id(ctx.project_id)
            except ValueError:
                return _tool_error(400, "invalid project id")
            if target_project != selected_project:
                return _tool_error(403, "project selector does not match connection")
    if ctx.principal.mode == "team" and name in _TEAM_UNSCOPED_HOST_PATH_TOOLS:
        # Fail closed before dispatch — placed AFTER the project-selector check
        # so a mismatched selector still reports the selector error — because
        # the tool's host-path argument is not yet constrained to the caller's
        # workspace. Local mode is unaffected (handled by the local short-
        # circuit in _authorize_tool).
        return _tool_error(
            403, "tool is unavailable in team mode until its host path is scoped")
    if ctx.principal.mode == "team" and name == "project_list":
        visible = get_workspace_service().list_projects_for_user(
            ctx.principal.user_id
        )
        return CallToolResult(content=[TextContent(
            type="text",
            text=json.dumps({"projects": visible, "current": ctx.project_id}),
        )])

    if ctx.principal.mode == "team" and name == "project_create":
        args = arguments or {}
        try:
            pid = canonical_project_id(args.get("project_id"))
        except ValueError:
            return _tool_error(400, "invalid project id")
        service = get_workspace_service()
        requested_workspace = str(args.get("workspace_id") or "").strip()
        eligible = [
            membership
            for membership in service.list_memberships_for_user(ctx.principal.user_id)
            if role_allows(membership.role, "admin")
            and (
                not requested_workspace
                or membership.workspace_id == requested_workspace
            )
        ]
        if not eligible:
            return _tool_error(403, "workspace admin access required")
        if len(eligible) != 1:
            return _tool_error(
                422,
                "workspace_id is required when the caller administers multiple workspaces",
            )
        workspace_id = eligible[0].workspace_id
        owned = service.project_workspace(pid)
        if owned is None and (PROJECTS_DIR / pid).is_dir():
            return _tool_error(
                409,
                "existing unowned project must be bound by a workspace admin",
            )
        try:
            service.reserve_project(pid, workspace_id)
        except ProjectOwnershipConflict:
            return _tool_error(409, "project is owned by another workspace")
        except ValueError as exc:
            return _tool_error(400, str(exc) or "invalid project id")
        from prism_service.project_context import create_project

        try:
            create_project(pid)
        except Exception:
            return _tool_error(500, "project creation failed")
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({
            "created": pid,
            "message": f"Project '{pid}' created. Connect with ?project={pid}",
        }))])

    result = await handle_tool(name, arguments, project_id=ctx.project_id)
    # Normalize the success path to a CallToolResult so a DIRECT call_tool()
    # caller always gets a consistent shape (the SDK transport already wraps a
    # bare content list; existing consumers are shape-agnostic).
    if isinstance(result, CallToolResult):
        return result
    return CallToolResult(content=list(result))


# ---------------------------------------------------------------------------
# StreamableHTTP transport wiring
# ---------------------------------------------------------------------------

session_manager = StreamableHTTPSessionManager(
    app=server,
    stateless=True,
)


async def handle_mcp(scope, receive, send):
    """Raw ASGI handler for MCP requests.

    Extracts ?project=<id> from the query string and sets it as the
    current project for all tool calls on this connection.

    Mount strips the matched prefix from scope["path"], but the
    StreamableHTTPSessionManager expects the *original* path.  We
    reconstruct it so content-type negotiation works correctly.
    """
    # Reconstruct full path: Mount sets root_path / path relative to mount
    original_path = scope.get("root_path", "") + scope.get("path", "")
    if not original_path:
        original_path = "/mcp/"
    scope = dict(scope, path=original_path)

    qs = parse_qs(scope.get("query_string", b"").decode())
    project_id = qs.get("project", [DEFAULT_PROJECT])[0]
    tool_profile = qs.get("tool_profile", qs.get("profile", ["interactive"]))[0]
    authorization = ""
    for key, value in scope.get("headers", []):
        if key.lower() == b"authorization":
            authorization = value.decode("latin-1")
            break
    # The MCP endpoint is bound on the same 0.0.0.0 socket as /api, so it needs
    # the SAME remote rule: loopback trusted, anyone else presents the key.
    # Closing /api alone would have left this wide open (ASGI gives the peer as
    # scope["client"] = (host, port); a missing entry means no peer info).
    peer = scope.get("client") or None
    client_host = peer[0] if peer else None
    try:
        principal = AuthService(get_workspace_service()).resolve_principal(
            authorization or None, client_host=client_host
        )
    except AuthenticationRequired as exc:
        await JSONResponse(
            {"error": str(exc) or "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )(scope, receive, send)
        return
    if principal.mode == "team":
        try:
            project_id = canonical_project_id(project_id)
        except ValueError:
            await JSONResponse(
                {"error": "invalid project id"}, status_code=400
            )(scope, receive, send)
            return
    request_ctx = PrismRequestContext(
        project_id=project_id,
        request_id=uuid.uuid4().hex,
        transport="mcp-http",
        tool_profile=tool_profile,
        principal=principal,
    )

    # Force `Connection: close` on every MCP response so uvicorn closes
    # the TCP socket and sends FIN immediately after the response body
    # — instead of holding the socket in keep-alive and stranding it in
    # CLOSE_WAIT when the (short-lived hook) client closes its side
    # first. See issue #64: stateless StreamableHTTP doesn't reuse
    # connections anyway, and ~3-5 requests per session-start hook
    # otherwise leak a socket apiece.
    with use_request_context(request_ctx):
        await session_manager.handle_request(
            scope, receive, _wrap_send_with_close(send)
        )


def _wrap_send_with_close(send):
    """Return an ASGI ``send`` wrapper that injects ``Connection: close``
    on every ``http.response.start`` message. See issue #64."""
    async def _send(message):
        if message.get("type") == "http.response.start":
            headers = [
                (k, v) for k, v in (message.get("headers") or [])
                if k.lower() != b"connection"
            ]
            headers.append((b"connection", b"close"))
            message = {**message, "headers": headers}
        await send(message)
    return _send


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    async with session_manager.run():
        yield


starlette_app = Starlette(
    lifespan=lifespan,
    routes=[
        Mount("/mcp", app=handle_mcp),
    ],
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_mcp_server(port: int) -> None:
    """Start the MCP StreamableHTTP server on the given port (blocking)."""
    uvicorn.run(
        starlette_app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
