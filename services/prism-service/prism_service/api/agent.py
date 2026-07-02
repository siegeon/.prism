"""PI agent tool passthrough — the omnipresent left-rail agent's ONE door
into PRISM's MCP tool surface (task 711d5235).

The browser-side pi agent (@earendil-works/pi-agent-core) does its own tool
calling; each tool call lands here and dispatches IN-PROCESS via
mcp.tools.handle_tool — same semantics as the MCP HTTP endpoint minus the
localhost round-trip. Gated to a whitelist so the panel can never reach
admin/destructive tools no matter what the model emits.
"""

import json

from fastapi import APIRouter, Body, HTTPException, Query

router = APIRouter()

# The PI agent's reachable surface. Additions are a deliberate decision,
# not a default — this is EXACTLY the expert catalog exported by
# web/pi-expert.mjs (task e70cdcda: PI ships pre-loaded as the PRISM
# expert; the whitelist and the catalog move together). Deliberately
# EXCLUDED admin/destructive tools: janitor_*, prism_sync, okf_index /
# okf_get / okf_graph, prism_onboard, register_claude_source.
AGENT_TOOL_WHITELIST = frozenset({
    # Brain — retrieval + code-graph navigation.
    "brain_search",
    "brain_understand",
    "brain_find_symbol",
    "brain_outline",
    "brain_find_references",
    "brain_call_chain",
    # Memory — recall-before-store discipline lives in the expert prompt.
    "memory_recall",
    "memory_store",
    "memory_invalidate",
    # Tasks — the tracker PI orchestrates.
    "task_list",
    "task_next",
    "task_create",
    "task_update",
    # Conductor — the SDLC state machine. Panel-reachable per owner
    # directive (task e70cdcda): PI IS the orchestrator.
    "conductor_advance",
    "conductor_gate",
    "workflow_state",
    # Context + health.
    "context_bundle",
    "prism_status",
})

# Task 9f20b605 gated the conductor mutations to internal callers
# (internal=true body flag). Task e70cdcda RETIRES that gate per owner
# directive — PI is the orchestrator, so task_update / conductor_advance /
# conductor_gate are panel-reachable. The seam stays (an empty frozenset,
# still enforced below) for future admin-only tools.
INTERNAL_ONLY_TOOLS: frozenset[str] = frozenset()


@router.post("/tool")
async def call_tool(
    project: str = Query("default"), body: dict = Body(...),
) -> dict:
    name = body.get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(422, "tool 'name' required")
    if name not in AGENT_TOOL_WHITELIST:
        raise HTTPException(
            403, f"tool {name!r} is not whitelisted for the PI agent",
        )
    # Internal-only gate (task 9f20b605 FR-3): conductor mutations require
    # a LITERAL internal=true — truthy look-alikes ("true", 1) do not open
    # the gate, so the browser panel surface is provably unchanged.
    if name in INTERNAL_ONLY_TOOLS and body.get("internal") is not True:
        raise HTTPException(
            403,
            f"tool {name!r} is for internal agent callers only "
            "(requires internal=true)",
        )
    args = body.get("args") or {}
    if not isinstance(args, dict):
        raise HTTPException(422, "'args' must be an object")

    from prism_service.mcp.tools import handle_tool

    contents = await handle_tool(name, args, project_id=project)
    # handle_tool returns list[TextContent]; tools emit JSON text. Surface
    # it parsed so the panel renders structure, with the raw text as the
    # fallback for non-JSON payloads.
    texts = [getattr(c, "text", "") for c in contents]
    raw = "\n".join(t for t in texts if t)
    return {"name": name, "result": _parse_payload(texts[0] if texts else ""), "raw": raw}


def _parse_payload(text: str):
    """Tool text is JSON, but handle_tool may prepend the PRISM_REFLECTION
    nudge header (a Claude-session affordance, noise to the PI panel). Parse
    the JSON payload from the first brace/bracket; fall back to raw text."""
    for start in (0, *(i for i in (text.find("{"), text.find("[")) if i > 0)):
        try:
            return json.loads(text[start:])
        except ValueError:
            continue
    return text
