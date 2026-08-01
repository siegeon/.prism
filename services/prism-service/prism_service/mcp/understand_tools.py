"""MCP tool surface for the v5.1 Understand-Anything layer (T9).

Lives as a sidecar to `prism_service.mcp.tools` so the >3000-line main file
stays roughly readable. The main `tools.py` imports
`UNDERSTAND_TOOLS`, `UNDERSTAND_TOOL_NAMES`, and `dispatch` and
splices them into its registration list + dispatcher.

Four tools remain, all load-bearing:

* **Trigger / write** — `understand_refresh`, `understand_drain_queue`,
  `understand_store_result`.
* **Read** — `understand_status`.

`understand_refresh` and `understand_status` are called by
`assets/stop_record_hook.py` over the automation profile; drain_queue and
store_result back the analyzer worker path.

Task 4899173a retired the other six (`understand_bootstrap`,
`understand_configure`, `understand_get_tour`, `understand_get_layers`,
`understand_get_domains`, `understand_get_onboarding`) — the v5.1 analysis
queue's cold-start and artifact-read half, superseded by the okf_* Understand
wiki (`okf_index` / `okf_get` / `okf_graph`). None had a consumer outside its
own tests; see the RETIRE rows of `docs/mcp-tool-usage-ledger.md`.

Every response is wrapped in an envelope:

    {"data": <payload>, "meta": {"sha": "...", "built_at": <ts>,
                                  "age_seconds": <int>}}

so callers (UI, agents) can flag stale answers without re-querying.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from mcp.types import TextContent, Tool

from prism_service.engines import understand_engine as ue
from prism_service.services import understand_artifact_store as store


def _project_schema(extra: Optional[dict] = None) -> dict:
    props: dict = {
        "project": {
            "type": "string",
            "description": "Project id; defaults to the request's ?project=",
        }
    }
    if extra:
        props.update(extra)
    return {"type": "object", "properties": props}


UNDERSTAND_TOOLS: list[Tool] = [
    Tool(
        name="understand_refresh",
        description=(
            "Plan + enqueue an Understand-Anything refresh for the project. "
            "Idempotent: re-running before drain returns the same job ids."
        ),
        inputSchema=_project_schema({
            "analyzers": {
                "type": "array", "items": {"type": "string"},
                "description": "Subset of analyzers; defaults to all four.",
            },
        }),
    ),
    Tool(
        name="understand_status",
        description=(
            "Report tracked ref, current SHA, last analyzed SHA, queue "
            "depth, cached SHAs, and budget settings."
        ),
        inputSchema=_project_schema(),
    ),
    Tool(
        name="understand_drain_queue",
        description=(
            "Pop up to max_jobs pending jobs as work plans for the caller "
            "(a Claude session) to execute. Caller writes results back via "
            "understand_store_result. INV-6: in-session drain only."
        ),
        inputSchema=_project_schema({
            "max_jobs": {"type": "integer", "default": 1},
        }),
    ),
    Tool(
        name="understand_store_result",
        description=(
            "Persist a completed analyzer result through to the cache and "
            "the queue. Called by the session that ran claude -p."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "job_id": {"type": "string"},
                "analyzer": {"type": "string"},
                "target_sha": {"type": "string"},
                "payload": {
                    "description": "JSON dict for json analyzers, plain string for onboarding_writer",
                },
                "tokens_used": {"type": "integer", "default": 0},
                "wall_clock_s": {"type": "number", "default": 0.0},
                "status": {"type": "string", "default": "complete"},
                "error": {"type": "string", "default": ""},
            },
            "required": ["job_id", "analyzer", "target_sha", "payload"],
        },
    ),
]


# RETIRED (task 4899173a): understand_get_tour / _get_layers / _get_domains /
# _get_onboarding were the read half of the v5.1 analysis queue, superseded by
# the okf_* Understand wiki (okf_index / okf_get / okf_graph). Retired together
# with understand_bootstrap / understand_configure per the RETIRE rows of
# docs/mcp-tool-usage-ledger.md — none had a consumer outside its own tests.
# The four tools that remain below are load-bearing: refresh/status are called
# by assets/stop_record_hook.py over the automation profile, and
# drain_queue/store_result back the analyzer worker path.

# v6.6.x — the four-page knowledge model collapsed to TWO surfaces (Brain =
# code-graph viz; Understand = ONE unified wiki over curated memory). The
# understand_* analysis-queue tools predate that wiki; mark every one LEGACY so
# agents reach for okf_index/okf_get/okf_graph + brain_understand first.
# Behavior is unchanged — descriptions only.
_LEGACY_PREFIX = (
    "LEGACY (v5.1 analysis queue) — prefer okf_index/okf_get/okf_graph + "
    "brain_understand: "
)
UNDERSTAND_TOOLS = [
    t.model_copy(update={"description": _LEGACY_PREFIX + (t.description or "")})
    for t in UNDERSTAND_TOOLS
]

UNDERSTAND_TOOL_NAMES: set[str] = {t.name for t in UNDERSTAND_TOOLS}



def _envelope(data, sha: str, built_at: float = 0.0) -> dict:
    return {
        "data": data,
        "meta": {
            "sha": sha,
            "built_at": built_at,
            "age_seconds": int(time.time() - built_at) if built_at else None,
        },
    }


def _resolve_project(arguments: dict, default_project: str) -> str:
    return arguments.get("project") or default_project


def _json(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def _latest_sha(project: str) -> Optional[str]:
    """Return the last_analyzed_sha if set, else the newest cached
    SHA dir mtime, else None."""
    state = ue._read_state(project)
    sha = state.get("last_analyzed_sha")
    if sha:
        return sha
    cached = store.list_cached_shas(project)
    return cached[-1] if cached else None


def dispatch(
    name: str,
    arguments: dict,
    default_project: str,
) -> Optional[list[TextContent]]:
    """Handle one of the understand_* tools. Returns None if `name`
    isn't owned by this surface."""
    if name not in UNDERSTAND_TOOL_NAMES:
        return None

    project = _resolve_project(arguments, default_project)
    eng = ue.UnderstandEngine(project)

    if name == "understand_refresh":
        analyzers = arguments.get("analyzers") or None
        result = eng.refresh(analyzers=analyzers)
        return [TextContent(type="text", text=_json({
            "data": {
                "status": result.status,
                "current_sha": result.target_sha,
                "enqueued": len(result.queued),
                "cached_hits": result.cached_hits,
                "queued": result.queued,
                "job_ids": result.job_ids,
                "budget_used": result.budget_used,
                "budget_limit": result.budget_limit,
            },
            "meta": {"sha": result.target_sha, "built_at": time.time(),
                     "age_seconds": 0},
        }))]

    if name == "understand_status":
        return [TextContent(type="text", text=_json(
            _envelope(eng.status(), sha=eng.status().get("current_sha") or "")
        ))]

    if name == "understand_drain_queue":
        max_jobs = int(arguments.get("max_jobs", 1))
        claimed = eng.queue.drain(max_jobs=max_jobs)
        jobs_out = [
            {
                "job_id": j.id,
                "analyzer": j.analyzer,
                "target_sha": j.target_sha,
                "scope_hash": j.scope_hash,
                "attempts": j.attempts,
            }
            for j in claimed
        ]
        return [TextContent(type="text", text=_json({
            "data": {"jobs": jobs_out, "count": len(jobs_out)},
            "meta": {"sha": "", "built_at": time.time(), "age_seconds": 0},
        }))]

    if name == "understand_store_result":
        job_id = arguments["job_id"]
        analyzer = arguments["analyzer"]
        target_sha = arguments["target_sha"]
        status = (arguments.get("status") or "complete").lower()
        # "partial" = claude hit max-turns but emitted parseable output;
        # persist it as a usable artifact and mark queue completed.
        if status in ("complete", "partial"):
            store.put(
                project, target_sha, analyzer, arguments["payload"],
                tokens_used=int(arguments.get("tokens_used", 0)),
                wall_clock_s=float(arguments.get("wall_clock_s", 0.0)),
            )
            eng.queue.complete(job_id, result_path=str(
                store.sha_dir(project, target_sha)
            ))
            eng._mark_analyzed(target_sha)
        else:
            eng.queue.fail(job_id, error=arguments.get("error", "") or status)
        return [TextContent(type="text", text=_json({
            "data": {"stored": True, "status": status},
            "meta": {"sha": target_sha, "built_at": time.time(),
                     "age_seconds": 0},
        }))]

    return None
