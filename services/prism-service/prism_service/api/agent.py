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
    # Magic — conversational app onboarding (task 651cea3b). Routed to the
    # /api/magic/interview/converse logic below, NOT handle_tool: the tiny
    # model only relays text; converse() does the drafting/pairing/building.
    "magic_interview",
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

    # magic_interview is a customer-onboarding tool, not an MCP tool: it drives
    # the reverse-engineering interview + auto-build (task 651cea3b). The panel
    # proxies it here as a dumb passthrough; the server does the heavy lifting.
    if name == "magic_interview":
        from prism_service.services import magic_interview as mi
        try:
            result = mi.converse(
                project,
                description=args.get("description"),
                answers=args.get("answers"),
            )
        except Exception as exc:
            raise HTTPException(
                500, f"tool dispatch failed: {type(exc).__name__}: {exc}",
            )
        return {"name": name, "ok": True, "result": result,
                "raw": json.dumps(result)}

    from prism_service.mcp.tools import handle_tool

    # Task 4f76beb9 FR-1: an exception ESCAPING the dispatcher is a server
    # fault, not a tool result — surface it as a clean 500 detail (type +
    # message only, never a traceback body).
    try:
        contents = await handle_tool(name, args, project_id=project)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            500, f"tool dispatch failed: {type(exc).__name__}: {exc}",
        )
    # handle_tool returns list[TextContent]; tools emit JSON text. Surface
    # it parsed so the panel renders structure, with the raw text as the
    # fallback for non-JSON payloads.
    texts = [getattr(c, "text", "") for c in contents]
    raw = "\n".join(t for t in texts if t)
    # Task 4f76beb9 FR-2: the MCP layer reports dispatch errors IN-BAND as
    # a plain 'Error: ...' text (mcp.tools._dispatch_tool catch-all). The
    # pi agent loop needs a MACHINE-legible error it can repair from, not
    # stack noise dressed as a result — surface it as ok:false + error.
    err = _error_message(texts[0] if texts else "")
    if err is not None:
        return {"name": name, "ok": False, "error": err}
    return {
        "name": name,
        "ok": True,
        "result": _parse_payload(texts[0] if texts else ""),
        "raw": raw,
    }


@router.post("/run")
def record_run(
    project: str = Query("default"), body: dict = Body(...),
) -> dict:
    """Record ONE task-attributed PI-panel exchange into the pi_runs ledger
    (task fc08da8d).

    Unlike POST /tool (which dispatches an MCP tool), this endpoint takes
    the exchange's USAGE — the browser PI agent runs inference locally, so
    its tokens never reach a Claude transcript; the panel POSTs them here on
    agent_end. Recorded with backend='panel', purpose='panel-drive' and the
    task_id detected from the exchange's conductor/task tool calls, so the
    conductor tile burn + the task detail can attribute the PI agent's work
    to the driven task. Best-effort: a malformed payload is coerced, never
    500'd, mirroring the ledger's own never-raise discipline."""
    from prism_service.services import pi_run_log

    def _int(v) -> int:
        try:
            return max(0, int(v or 0))
        except (TypeError, ValueError):
            return 0

    def _float(v) -> float:
        try:
            return max(0.0, float(v or 0))
        except (TypeError, ValueError):
            return 0.0

    input_tokens = _int(body.get("input_tokens"))
    output_tokens = _int(body.get("output_tokens"))
    tools = body.get("tools_used")
    run_id = pi_run_log.record_run(
        backend="panel",
        model=str(body.get("model") or ""),
        purpose="panel-drive",
        project=project,
        task_id=str(body.get("task_id") or ""),
        tools_used=tools if isinstance(tools, list) else [],
        duration_ms=_float(body.get("ms")),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        # `tokens` is the completion-side KPI the ledger/strip reads.
        tokens=output_tokens,
        turns=1,
        ok=bool(body.get("ok", True)),
    )
    return {"ok": run_id is not None, "run_id": run_id}


def _error_message(text: str) -> str | None:
    """Extract the in-band dispatch-error message, or None for a real
    result. mcp.tools._dispatch_tool's catch-all emits `Error: <Type>:
    <msg>` (also `Error: Unknown tool '...'`); handle_tool may prepend the
    PRISM_REFLECTION_PENDING nudge header (terminated by a `---` line), so
    strip that before matching. Only a payload that STARTS with the
    contract prefix counts — JSON results containing 'Error:' are not
    errors."""
    body = text or ""
    first_line = body.split("\n", 1)[0]
    if "PRISM_REFLECTION_PENDING" in first_line:
        marker = "\n---\n"
        if marker in body:
            body = body.split(marker, 1)[1]
    if body.startswith("Error: "):
        return body[len("Error: "):].strip()
    return None


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
