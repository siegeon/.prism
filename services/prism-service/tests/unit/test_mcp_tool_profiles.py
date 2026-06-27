"""Tests for MCP tool-profile filtering."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _names(profile: str) -> set[str]:
    from prism_service.mcp.tools import tools_for_profile

    return {tool.name for tool in tools_for_profile(profile)}


def test_interactive_profile_exposes_core_agent_tools_only():
    names = _names("interactive")

    # 17 original core tools + 2 Conductor v2 tools
    # (conductor_advance / conductor_gate) + 10 v5.1 understand_* tools
    # (T9 nine + understand_configure follow-up) + brain_understand
    # (v6.1.6 Ultimate Graph merge retrieval) + task_link_session
    # (v6.2.9 task<->session association — rides the task_* family)
    # + register_claude_source (v6.3.16 — Claude reports its own
    # transcript source instead of slug-guessing; task b6650506)
    # + okf_index / okf_get (v6.6.2 Understand wiki) + okf_graph
    # (v6.6.6 — the wiki's concept graph; task a4995b7a)
    # + principles_seed (v6.7.1 — seed architecture principles so a fresh
    # project's plan_gate is satisfiable; GH #171).
    # + janitor_check / janitor_submit / janitor_abandon / memory_invalidate
    # (v6.7.1 / GH #173 — prism-reflect sub-agent allow-list served here so
    # PRISM_REFLECTION_PENDING candidates are actionable).
    # + prism_onboard (v6.7.1 / GH #172 — one-call bootstrap: seed principles
    # + return the .mcp.json snippet/ports/version/prism_guide pointer).
    assert len(names) == 41
    assert "prism_onboard" in names
    assert "brain_understand" in names
    assert "task_link_session" in names
    assert "register_claude_source" in names
    # GH #173 — reflection janitor tooth is now served interactively.
    assert {
        "janitor_check",
        "janitor_submit",
        "janitor_abandon",
        "memory_invalidate",
    } <= names
    assert {
        "brain_search",
        "brain_call_chain",
        "memory_recall",
        "task_next",
        "workflow_state",
        "conductor_advance",
        "conductor_gate",
        "context_bundle",
        "prism_status",
        "understand_refresh",
        "understand_status",
        "understand_drain_queue",
    } <= names
    assert {
        "brain_index_doc",
        "record_session_outcome",
        "meta_conductor_auto",
        "janitor_enqueue",
        "project_onboard",
        "verifier_run",
    }.isdisjoint(names)


def test_profile_aliases_are_stable():
    assert _names("core") == _names("interactive")
    assert _names("project") == _names("admin")
    assert _names("telemetry") == _names("hooks")
    assert _names("hooks_api") == _names("automation")


def test_automation_profile_exposes_hook_owned_tools_only():
    names = _names("automation")

    assert {
        "prism_status",
        "prism_refresh",
        "graph_rebuild",
        "task_list",
        "task_update",
        "brain_search_feedback",
        "record_session_outcome",
        "record_skill_usage",
        "record_subagent_outcome",
        "janitor_check",
        "janitor_mark_stale",
        "janitor_enqueue",
        "verifier_run",
    } <= names
    assert {
        "project_onboard",
        "project_create",
        "meta_conductor_auto",
        "janitor_submit",
        "brain_index_doc",
    }.isdisjoint(names)


def test_all_profile_keeps_explicit_full_surface():
    from prism_service.mcp.tools import TOOLS

    assert _names("all") == {tool.name for tool in TOOLS}


def test_default_profile_uses_interactive_surface():
    from prism_service.mcp.request_context import PrismRequestContext

    assert PrismRequestContext().tool_profile == "interactive"
    assert _names(None) == _names("interactive")
    assert _names("default") == _names("interactive")


def test_unknown_profile_falls_back_to_interactive_tools():
    assert _names("does-not-exist") == _names("interactive")


def test_conductor_v2_tools_registered_with_required_schema():
    """conductor_advance / conductor_gate appear in TOOLS with the
    required input fields (id; action enum for the gate tool)."""
    from prism_service.mcp.tools import TOOLS

    by_name = {tool.name for tool in TOOLS}
    assert {"conductor_advance", "conductor_gate"} <= by_name

    tools_map = {tool.name: tool for tool in TOOLS}
    advance = tools_map["conductor_advance"]
    assert advance.inputSchema["required"] == ["id"]
    assert "validation" in advance.inputSchema["properties"]

    gate = tools_map["conductor_gate"]
    # v6.0.32: reason is now required on every gate_decide call.
    assert set(gate.inputSchema["required"]) == {"id", "action", "reason"}
    assert gate.inputSchema["properties"]["action"]["enum"] == [
        "approve", "reject",
    ]


def test_default_profile_blocks_hidden_tool_calls():
    import asyncio

    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.server import call_tool

    with use_request_context(PrismRequestContext(tool_profile="interactive")):
        result = asyncio.run(call_tool("brain_index_doc", {"path": "x", "content": "y"}))

    # Shape-agnostic extraction: pre-fix call_tool returns a bare
    # list[TextContent]; post-fix (GH #99 part 3) it returns
    # types.CallToolResult. Either way the structured JSON rejection
    # payload is in the (joined) text. The isError=true contract itself
    # is pinned by tests/unit/test_mcp_profile_rejection_iserror.py,
    # which drives the SDK's real CallToolRequest handler.
    content = getattr(result, "content", result)
    if not isinstance(content, (list, tuple)):
        content = [content]
    text = "".join(getattr(b, "text", "") or "" for b in content)
    payload = json.loads(text)
    assert payload["error"] == "Tool is not available for this MCP tool profile."
    assert payload["tool"] == "brain_index_doc"
    assert payload["tool_profile"] == "interactive"
