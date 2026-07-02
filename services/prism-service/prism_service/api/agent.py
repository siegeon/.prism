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

# The PI panel's reachable surface. Additions are a deliberate decision,
# not a default — see the task plan (FR-4).
AGENT_TOOL_WHITELIST = frozenset({
    "brain_search",
    "memory_recall",
    "memory_store",
    "task_list",
    "task_create",
    "prism_status",
    # Conductor surface (task 9f20b605, phase 2 of the pi internal agent):
    # deliberate additions so a DIRECTED pi job can drive SDLC work —
    # author/patch plan_doc (task_update), advance the per-task state
    # machine (conductor_advance), propose gate evidence (conductor_gate).
    # All three are INTERNAL-ONLY (see INTERNAL_ONLY_TOOLS below).
    "task_update",
    "conductor_advance",
    "conductor_gate",
})

# INTERNAL AGENT CALLERS ONLY (task 9f20b605 FR-3): these mutate the SDLC
# state machine, so they require an explicit internal=true body flag. The
# browser PI panel never sends the flag — its reachable surface stays
# read/create-only no matter what the model emits. The subprocess runner
# (web/pi-runtime.mjs) sends internal=true on every bridged call.
INTERNAL_ONLY_TOOLS = frozenset({
    "task_update",
    "conductor_advance",
    "conductor_gate",
})


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
