"""/api/chat — the internal AI agent's chat surface (task: chat-ops panel).

Answers a single turn synchronously inside the HTTP request, via the same
`claude_cli.invoke` primitive every autonomous bot step already uses
(services/task_runner.py) — this is deliberately NOT routed through the
in-daemon task runner's WORKFLOWS registry: that ticks every
PRISM_TASK_RUNNER_INTERVAL seconds (minutes), which is far too slow for an
interactive panel a human is staring at. See services/chat_service.py for
why conversations live in their own table instead of the SDLC tasks store.

Auth follows the standard bearer/access-key path (api.auth.current_principal)
that every other /api/* route already enforces — no new auth mechanism.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from prism_service.api.auth import coerce_principal, current_principal
from prism_service.inference import claude_cli
from prism_service.models.workspace import Principal
from prism_service.project_context import get_project

router = APIRouter()

# Narrow, no-tool call (see claude_cli.invoke: allowed_tools=() puts the run
# in a clean room with none of the ~20k tokens of interactive-agent tooling
# doctrine) — a chat reply should be fast and cannot edit the repo. Budget
# and timeout are sized for a synchronous HTTP request a human is watching,
# not an autonomous multi-turn SDLC step.
_MAX_TURNS = 4
_MAX_BUDGET_USD = 0.35
_TIMEOUT_S = 90.0
_HISTORY_TURNS = 20

# PRISM_INFERENCE_BACKEND=local (this dev topology) routes claude_cli.invoke through a
# local reasoning model whose <think>...</think> scratchpad final_text() does not strip;
# the real Claude CLI never emits this, so it's a harmless no-op against that backend.
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

_SYSTEM_PREAMBLE = (
    "You are Prism, the internal AI agent embedded in the AOS app (an "
    "internal Bespoke Labs tool combining a PRISM knowledge/task layer with "
    "a Magic Cloud low-code backend). You are talking directly to the "
    "person currently using the app. "
    "Reply in 2-4 sentences by default — this is a chat panel, not a report. "
    "Do not produce headings, numbered lists, or multi-section breakdowns unless "
    "the user explicitly asks for a list or detailed writeup. If [Relevant indexed "
    "context] is provided below, ground your answer in it and prefer it over general "
    "knowledge — name the specific real things it shows (file names, concrete bot/"
    "component names) rather than describing them generically; a short answer that "
    "names real files and real names beats a longer one that doesn't. Do not restate "
    "or dump the raw context — synthesize, but stay concrete. "
    "You do not have live tool access in this conversation — you cannot "
    "read files, run code, or call Magic's Hyperlambda generator here — so "
    "never claim to have just done something; instead tell the user what "
    "to do or what you'd need to actually do it. "
    "If you are told the user is viewing SQL Studio, you can see the database, table, "
    "and query they currently have open below — when you propose or correct a SQL query "
    "for them, put ONLY that query in a fenced ```sql code block (nothing else in the "
    "fence) so the app can offer to place it directly into their editor; put any "
    "explanation outside the fence."
)


class ChatContext(BaseModel):
    route: str = ""
    title: str = ""
    entity: str = ""


class ChatRequest(BaseModel):
    message: str
    project: str = "default"
    conversation_id: str | None = None
    context: ChatContext = ChatContext()


# Caps the grounding block's token cost: a handful of short snippets, not whole files —
# this is meant to orient the model toward the real source, not replace giving it tool
# access. Measured against the "what are Magic bots" case: 4 results at 600 chars each
# adds well under 1k tokens versus the ~150-token no-context baseline, still an order of
# magnitude cheaper than a filesystem-research agent's multi-file read/grep session.
_BRAIN_RESULTS = 6
_SNIPPET_CHARS = 600

# AOS is one product across three physically separate git repos — the Blazor client
# (project "aos"), PRISM's own service ("prism"), and the Magic Cloud backend ("magic").
# PRISM's brain indexes one repo per project, so a single-project search structurally
# cannot see the other two: measured directly (2026-09-17), a "what are Magic bots"
# question against project="prism" alone came back with zero hits from Magic's backend
# and an internally-contradictory answer ("bots aren't stored in a database", when a
# real bots.db legacy schema exists there). Searching all three and merging by score is
# still one search per project (cheap, no LLM cost) feeding the same single grounding
# block — the user only ever sees one internal agent, not three.
_FEDERATED_PROJECTS = ("prism", "magic", "aos")
_PER_PROJECT_RESULTS = 4

# A short, hand-verified correction for a specific gap the federated search above does not
# close: PRISM's ingest filters skip Magic's .hl (Hyperlambda) module files entirely, so the
# legacy bots.db schema (superseded by the JSON-workflow system above but still real and
# still live for the "architect" bot) never surfaces via search. Verified directly against
# the live schema (2026-09-17): `sqlite3 .../bots.db .schema` lists exactly these tables.
# This is a maintained fact, not a search result — update it if the schema changes.
_BOT_SCHEMA_NOTE = (
    "[Known fact, verified against the live schema — state this plainly if asked about bot "
    "storage, do not claim bots are code-only or database-free] There are two real bot "
    "mechanisms: (1) the current/primary JSON-workflow system — each bot is a bot.json "
    "declaring an FSM plus allowed behaviors, each behavior a JSON step-graph, run by "
    "WorkflowRuntime.cs/WorkflowController.cs/MagicStepExecutor.cs in Magic's backend "
    "workflows/ dir; and (2) a legacy SQLite bots.db (tables: bots, bot_fsms, bot_states, "
    "bot_transitions, bot_operations, bot_invocations, bot_results) used by Magic's older "
    "/magic/modules/bots/invoke-operation endpoint for one-off dispatch — narrower and "
    "only partially populated today. A Behavior is specifically a bounded Workflow a Bot "
    "calls to do one piece of work that may need inference — not a generic 'workflow "
    "transition'; do not conflate the two."
)


def _build_grounding(project: str, message: str) -> str:
    seen_projects = {project, *_FEDERATED_PROJECTS} if project in _FEDERATED_PROJECTS else {project}
    results: list[dict] = []
    for proj in seen_projects:
        try:
            hits = get_project(proj).brain_svc.search(message, limit=_PER_PROJECT_RESULTS)
        except Exception:
            continue
        results.extend(hits or [])
    results.sort(key=lambda r: r.get("rrf_score") or 0, reverse=True)
    results = results[:_BRAIN_RESULTS]

    lines = []
    if results:
        lines.append("[Relevant indexed context — cite it if it answers the question, ignore it if not]")
        for r in results:
            content = (r.get("content") or "").strip()
            if not content:
                continue
            snippet = content[:_SNIPPET_CHARS] + ("…" if len(content) > _SNIPPET_CHARS else "")
            lines.append(f"--- {r.get('source_file', r.get('doc_id', 'unknown'))} ---\n{snippet}")

    haystack = (message + " " + " ".join(r.get("source_file", "") for r in results)).lower()
    if "bot" in haystack or "workflow" in haystack:
        lines.append(_BOT_SCHEMA_NOTE)

    return "\n".join(lines)


def _build_prompt(history: list[dict], context: ChatContext, message: str, grounding: str = "") -> str:
    lines = [_SYSTEM_PREAMBLE, ""]
    where = ", ".join(
        f"{label}: {value}"
        for label, value in (("page", context.route), ("section", context.title), ("viewing", context.entity))
        if value
    )
    if where:
        lines.append(f"[The user is currently on: {where}]")
    if grounding:
        lines.append(grounding)
    for turn in history:
        speaker = "User" if turn["role"] == "user" else "You"
        lines.append(f"{speaker}: {turn['content']}")
    lines.append(f"User: {message}")
    lines.append("You:")
    return "\n".join(lines)


@router.post("")
def send(body: ChatRequest = Body(...), principal: Principal = Depends(current_principal)) -> dict:
    coerce_principal(principal)
    message = body.message.strip()
    if not message:
        raise HTTPException(422, "message must not be empty")

    project_ctx = get_project(body.project)
    chat_svc = project_ctx.chat_svc
    conversation_id = chat_svc.ensure_conversation(body.conversation_id, title=message)
    history = chat_svc.history(conversation_id, limit=_HISTORY_TURNS)

    grounding = _build_grounding(body.project, message)
    prompt = _build_prompt(history, body.context, message, grounding)
    try:
        result = claude_cli.invoke(
            prompt, work_dir=".", plugin_dir=".",
            allowed_tools=(), max_turns=_MAX_TURNS, max_budget_usd=_MAX_BUDGET_USD,
            timeout_s=_TIMEOUT_S, project=body.project, purpose=f"chat-ops#{conversation_id[:8]}",
        )
    except Exception as exc:
        raise HTTPException(502, f"the internal agent could not be reached: {exc}") from None

    reply = _THINK_BLOCK.sub("", result.final_text()).strip()
    if not reply:
        raise HTTPException(502, "the internal agent returned no reply")

    context_json = body.context.model_dump_json()
    chat_svc.append(conversation_id, "user", message, context_json)
    chat_svc.append(conversation_id, "assistant", reply)

    return {"conversation_id": conversation_id, "reply": reply}


@router.get("/{conversation_id}")
def get_history(conversation_id: str, project: str = "default",
                 principal: Principal = Depends(current_principal)) -> dict:
    coerce_principal(principal)
    chat_svc = get_project(project).chat_svc
    return {"conversation_id": conversation_id, "messages": chat_svc.history(conversation_id, limit=200)}
