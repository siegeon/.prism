"""PRISM MCP server — StreamableHTTP transport with per-project scoping.

The project is determined by the ?project= query parameter on the request URL.
E.g.  http://localhost:7777/mcp?project=my-app

If omitted, the "default" project is used.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from urllib.parse import parse_qs

import uvicorn
from mcp.types import CallToolResult, TextContent
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from prism_service.config import DEFAULT_PROJECT
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
from prism_service.services.workspace_service import (
    AuthorizationDenied,
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
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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
    minimum = (
        "admin" if name == "project_create"
        else "viewer" if name in _MCP_VIEWER_TOOLS
        else "member"
    )
    try:
        return get_workspace_service().require_project_role(
            ctx.principal.user_id, ctx.project_id, minimum
        )
    except AuthorizationDenied:
        return _tool_error(403, "project access denied")


@server.list_tools()
async def list_tools():
    """Return tools enabled for the current request profile."""
    return tools_for_profile(get_request_context().tool_profile)


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch a tool call to the handler, scoped to the current project."""
    ctx = get_request_context()
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
    if ctx.principal.mode == "team" and name == "project_list":
        visible = get_workspace_service().list_projects_for_user(
            ctx.principal.user_id
        )
        return CallToolResult(content=[TextContent(
            type="text",
            text=json.dumps({"projects": visible, "current": ctx.project_id}),
        )])

    if ctx.principal.mode == "team" and name == "project_create":
        pid = str((arguments or {}).get("project_id") or "").strip()
        if not _PROJECT_ID_RE.fullmatch(pid):
            return _tool_error(400, "invalid project id")
        service = get_workspace_service()
        current_workspace = service.project_workspace(ctx.project_id)
        if current_workspace is None:
            return _tool_error(403, "current project has no workspace owner")
        owned = service.project_workspace(pid)
        if owned is not None and owned.id != current_workspace.id:
            return _tool_error(409, "project is owned by another workspace")
        from prism_service.project_context import create_project

        create_project(pid)
        service.bind_project(pid, current_workspace.id)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({
            "created": pid,
            "message": f"Project '{pid}' created. Connect with ?project={pid}",
        }))])

    return await handle_tool(name, arguments, project_id=ctx.project_id)


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
    try:
        principal = AuthService(get_workspace_service()).resolve_principal(
            authorization or None
        )
    except AuthenticationRequired:
        await JSONResponse(
            {"error": "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )(scope, receive, send)
        return
    if principal.mode == "team":
        try:
            membership = get_workspace_service().require_project_role(
                principal.user_id, project_id, "viewer"
            )
        except AuthorizationDenied:
            await JSONResponse(
                {"error": "project access denied"}, status_code=403
            )(scope, receive, send)
            return
        principal = replace(principal, role=membership.role)
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
