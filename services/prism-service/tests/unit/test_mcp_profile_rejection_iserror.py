"""Profile-rejection must surface as CallToolResult.isError=true (GH #99 part 3).

The dispatcher used to return a bare list of TextContent for a
profile-rejected (or unknown) tool. A bare content list flows through
the MCP SDK's *success* path and the framework stamps isError=False —
so the client sees a false-success and a dropped telemetry write goes
unnoticed (exactly how the original 38 record_session_outcome drops
hid). These tests drive the REAL server.call_tool path through the
SDK's registered CallToolRequest handler so we observe the isError flag
the client would actually receive — not just the raw function return.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _dispatch(name: str, profile: str = "interactive", arguments=None):
    """Invoke a tool through the SDK's real CallToolRequest handler.

    Returns the resolved CallToolResult (with .isError computed by the
    framework) — the exact object a connected MCP client would see.
    """
    from mcp import types

    from prism_service.mcp.request_context import (
        PrismRequestContext,
        use_request_context,
    )
    from prism_service.mcp.server import server

    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(
            name=name, arguments=arguments or {}
        ),
    )
    with use_request_context(PrismRequestContext(tool_profile=profile)):
        server_result = asyncio.run(handler(req))
    return server_result.root


def test_profile_rejected_tool_surfaces_iserror_true():
    """AC #3 (red today): a maintenance-only tool invoked over the
    'interactive' profile must come back through the dispatcher as
    isError=true while STILL carrying the helpful rejection payload.
    Pre-fix the bare-list return is stamped isError=false (false success)."""
    result = _dispatch(
        "brain_index_doc",
        profile="interactive",
        arguments={"path": "x", "content": "y"},
    )

    assert result.isError is True, (
        "profile-rejected tool must report isError=true, not a "
        "false-success the client silently drops"
    )

    text = result.content[0].text
    assert "not available for this MCP tool profile" in text
    payload = json.loads(text)
    assert payload["tool"] == "brain_index_doc"
    assert payload["tool_profile"] == "interactive"
    assert "hint" in payload  # reconnect hint preserved


def test_unknown_tool_name_surfaces_iserror_true():
    """AC #2 (red today): an unregistered tool name (in no profile set)
    must also surface isError=true — never a false-success — so a typo'd
    or removed tool name can't masquerade as a completed call."""
    result = _dispatch("this_tool_does_not_exist", profile="interactive")

    assert result.isError is True, (
        "unknown tool name must report isError=true, not false-success"
    )
    # Still a structured, human-readable explanation in the text.
    assert result.content and result.content[0].text.strip()
    assert "this_tool_does_not_exist" in result.content[0].text


def test_allowed_tool_is_not_flagged_iserror():
    """Guard against over-correction: a tool that IS in the profile and
    runs cleanly must remain isError=false (no false-failure regression)."""
    result = _dispatch("prism_status", profile="interactive")
    assert result.isError in (False, None), (
        "an allowed, successful tool call must not be flagged isError=true"
    )
