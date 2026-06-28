"""MCP tool definitions and handler for the PRISM service."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from mcp.types import Tool, TextContent

# v5.1 understand-anything tool surface (sidecar) — see T9.
from prism_service.mcp.understand_tools import (
    UNDERSTAND_TOOLS,
    UNDERSTAND_TOOL_NAMES,
    dispatch as _understand_dispatch,
)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="brain_search",
        description=(
            "Search the project knowledge base using hybrid BM25 + vector + graph "
            "search. Each result carries a `search_id` — after you've read or "
            "edited a result's source_file (or deliberately skipped it), call "
            "`brain_search_feedback(search_id, doc_id, signal='up'|'down')` to "
            "record whether the result was useful. That feedback is persisted "
            "to the searches/search_feedback tables for retrieval tuning."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "domain": {"type": "string", "description": "Filter by domain (py, ts, md, expertise)"},
                "limit": {"type": "integer", "description": "Max results", "default": 5},
                "domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by multiple domains",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="brain_index_doc",
        description=(
            "Index a document into the Brain knowledge base. Claude reads the file "
            "and sends the content — PRISM stores and indexes it for future search. "
            "Use for key source files, architecture docs, configs, README, etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Original file path (used as document ID, e.g. 'src/auth/middleware.ts')",
                },
                "content": {
                    "type": "string",
                    "description": "The file content or a summary of it",
                },
                "domain": {
                    "type": "string",
                    "description": "Content domain: code, docs, config, architecture, test, api",
                    "default": "code",
                },
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "kind": {"type": "string", "description": "function, class, interface, type, endpoint, etc."},
                        },
                    },
                    "description": "Key entities in the file (functions, classes, endpoints). Optional — helps graph search.",
                },
            },
            "required": ["path", "content"],
        },
    ),
    Tool(
        name="brain_understand",
        description=(
            "Ultimate Graph merge — ONE retrieval that fuses Brain search "
            "and the code graph into a single ranked, graph-aware view "
            "(siegeon/.prism#50). Prefer this over chaining brain_search + "
            "brain_find_references + brain_call_chain when you want to "
            "understand an area of the codebase: it returns the ranked hits "
            "AND their 1-hop neighbor subgraph AND a per-file context bundle "
            "(outline, callers, callees, matched chunks) in one call. Empty "
            "query = whole-graph overview ranked by PageRank centrality; "
            "typed query = focused subgraph around the matches. Shape: "
            "{ mode, nodes[], edges[], communities[], ranked[{entity_id, "
            "score, why}], context[{file, outline, references, call_chain, "
            "chunks, annotations}], open_questions[], layout_hint }. All "
            "structural data carries provenance='deterministic'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to understand. Omit/empty for a "
                    "whole-graph overview ranked by centrality.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max ranked hits / hubs (1-200)",
                    "default": 20,
                },
                "depth": {
                    "type": "integer",
                    "description": "Neighbor hops to pull into the subgraph "
                    "(0-3). 1 = direct callers/callees.",
                    "default": 1,
                },
                "seed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Seed the view from these files instead of "
                    "a text query — e.g. the members of a cluster you want "
                    "explained. Ranked by centrality within the set.",
                },
                "label": {
                    "type": "string",
                    "description": "Human label for a seed_files selection "
                    "(e.g. the cluster name).",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="register_claude_source",
        description=(
            "Tell PRISM where THIS Claude instance keeps its session "
            "transcripts for a project, instead of PRISM guessing from the "
            "host home + slug math. Claude knows its own cwd and config dir, "
            "so it can report the real ~/.claude/projects/<slug> folder. "
            "PRISM resolves resolve_claude_home()/projects/path_to_slug(cwd) "
            "(honoring CLAUDE_CONFIG_DIR over the home dir), validates the "
            "folder exists and holds *.jsonl transcripts, and persists it as "
            "the project's claude_project_dir so the 60s import poller reads "
            "the right folder regardless of slug skew or host. It ALSO persists "
            "the cwd as the project's source_path, so the LIVE conductor token "
            "/ burn graph (which resolves transcripts via source_path) reads "
            "the SAME folder — one registration drives imports AND the live "
            "token graph (fixes empty / wrong-session token graphs). Idempotent "
            "— re-registering updates both. Returns { ok, resolved_dir, "
            "source_path, jsonl_count, project }."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "PRISM project id to attach the source to.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Claude's working directory for the project "
                    "(slugified into the ~/.claude/projects/<slug> folder).",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional active Claude session id (advisory).",
                },
            },
            "required": ["project", "cwd"],
        },
    ),
    Tool(
        name="brain_search_feedback",
        description=(
            "Record thumbs-up (signal='up') or thumbs-down (signal='down') "
            "on a single doc_id returned by a prior brain_search call. Uses "
            "the search_id the caller receives on each brain_search result. "
            "Feedback is persisted to the search_feedback table and "
            "aggregated on the /retrievals UI. Use this after you've worked "
            "with a search result to record whether it was actually useful "
            "— the data feeds future retrieval tuning."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "search_id": {
                    "type": "integer",
                    "description": "The search_id returned on each brain_search result",
                },
                "doc_id": {
                    "type": "string",
                    "description": "Which retrieved doc_id the feedback is about",
                },
                "signal": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "up = useful, down = not useful",
                },
                "note": {
                    "type": "string",
                    "description": "Optional short reason (why it was good/bad)",
                },
            },
            "required": ["search_id", "doc_id", "signal"],
        },
    ),
    Tool(
        name="brain_find_symbol",
        description=(
            "Return the chunk(s) for a named function/class/method — the "
            "token-efficient alternative to Read-ing the whole parent "
            "file. Example: brain_find_symbol('_fts5_search') returns a "
            "~40-line chunk with file, line range, and body instead of "
            "the 2500-line brain_engine.py. Optional kind filter: "
            "function | class | method | module."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "entity name (function, class, method)"},
                "kind": {"type": "string",
                         "description": "optional filter: function|class|method|module"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="brain_outline",
        description=(
            "Return the symbol outline of a source file — list of "
            "entity_name/entity_kind/line_start/line_end with NO bodies. "
            "Costs ~200 tokens for a file that would be 15K tokens to "
            "Read. Use this to orient before deciding which specific "
            "chunks to fetch via brain_find_symbol."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source_file": {"type": "string",
                                 "description": "file path as indexed"},
            },
            "required": ["source_file"],
        },
    ),
    Tool(
        name="brain_find_references",
        description=(
            "Return the call sites of a named entity via the graph. "
            "Each result is {caller_name, caller_kind, caller_file, "
            "relation}. Use find_symbol() on a caller_name to fetch its "
            "chunk content. Replaces 'grep for foo(' with a semantic "
            "query that respects function boundaries. By default skips "
            "rationale-comment edges; set include_rationale=true to "
            "include them when surfacing intent metadata."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
                "include_rationale": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include rationale-comment edges "
                        "(rationale_for relation). Default false."
                    ),
                },
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="brain_call_chain",
        description=(
            "Bounded BFS over the call graph starting at ``entity``. "
            "Returns a flat edge list [{from, to, kind, relation, hop, "
            "direction}] so you can reconstruct call flow OR blast "
            "radius. By default follows only ``calls`` edges and walks "
            "forward (callees). Set direction='callers' to answer "
            "'who would break if I change this?' or direction='both' "
            "for full impact analysis."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entity": {"type": "string"},
                "depth": {"type": "integer", "default": 2,
                          "description": "max hops (default 2)"},
                "limit": {"type": "integer", "default": 50},
                "relation": {
                    "type": "string",
                    "default": "calls",
                    "description": (
                        "Edge-kind filter: 'calls' (default) follows "
                        "only call edges; '*' (or empty) includes "
                        "every relation kind; any other value (e.g. "
                        "'uses', 'inherits') filters to that one kind."
                    ),
                },
                "direction": {
                    "type": "string",
                    "enum": ["callees", "callers", "both"],
                    "default": "callees",
                    "description": (
                        "BFS direction. 'callees' (default) = forward "
                        "call flow; 'callers' = blast radius (who "
                        "calls this); 'both' = union, with each edge "
                        "tagged by how it was discovered."
                    ),
                },
            },
            "required": ["entity"],
        },
    ),
    Tool(
        name="record_session_outcome",
        description=(
            "Upsert one session_outcomes row for the current Claude Code "
            "session. Called by the Stop hook that prism_install ships. "
            "Fields: "
            "session_id, duration_s, tokens_used, files_read, "
            "files_modified, skills_invoked. Persists to scores.db so "
            "the /sessions UI can render it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "duration_s": {"type": "integer"},
                "tokens_used": {"type": "integer"},
                "files_read": {"type": "integer"},
                "files_modified": {"type": "integer"},
                "skills_invoked": {"type": "integer"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="record_skill_usage",
        description=(
            "Record one skill invocation. Called by the PostToolUse "
            "hook that prism_install ships, on Skill tool use. Feeds "
            "the Conductor's "
            "skill-ranking model."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "skill_name": {"type": "string"},
                "timestamp": {"type": "string",
                               "description": "ISO-8601; omit for now"},
            },
            "required": ["session_id", "skill_name"],
        },
    ),
    Tool(
        name="record_outcome",
        description=(
            "Persist one PSP-scored execution outcome. Used by the "
            "SubagentStop recorder that prism_install ships and by "
            "workflow-step recorders. Metrics dict accepts tokens_used, "
            "duration_s, "
            "retries, gate_passed, tests_passed, coverage_pct, "
            "traceability_pct, probe_accuracy."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt_id": {"type": "string"},
                "persona": {"type": "string",
                             "description": "sm | dev | qa | validator | ..."},
                "step_id": {"type": "string"},
                "metrics": {"type": "object"},
            },
            "required": ["prompt_id", "persona", "step_id"],
        },
    ),
    Tool(
        name="record_subagent_outcome",
        description=(
            "Persist one SFR (Structured Feedback Review) outcome from a "
            "validator sub-agent. Called by the SubagentStop recorder. "
            "Upsert by prompt_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt_id": {"type": "string"},
                "validator": {"type": "string",
                               "description": "sub-agent name"},
                "recommendation": {"type": "string",
                                    "description": "APPROVE | REVISE | PASS | FAIL | ..."},
                "evidence_count": {"type": "integer"},
                "certificate_complete": {"type": "integer",
                                          "description": "0 or 1"},
                "certificate_blocked": {"type": "integer",
                                         "description": "0 or 1"},
                "timed_out": {"type": "integer", "description": "0 or 1"},
                "tokens_used": {"type": "integer"},
                "duration_s": {"type": "number"},
            },
            "required": ["prompt_id", "validator", "recommendation"],
        },
    ),
    Tool(
        name="meta_conductor_brief",
        description=(
            "Return a deterministic prompt-optimization brief for one "
            "persona/step. PRISM supplies current scores, top/low outcomes, "
            "current prompt text, and promotion thresholds; the calling agent "
            "may use this to draft a candidate, but PRISM owns storage and "
            "promotion."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "persona": {"type": "string"},
                "step_id": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["persona", "step_id"],
        },
    ),
    Tool(
        name="meta_conductor_propose",
        description=(
            "Store a generated prompt variant as a Meta-Conductor candidate. "
            "This does not activate the prompt. Call meta_conductor_evaluate "
            "with benchmark/holdout metrics to let PRISM promote or reject it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "persona": {"type": "string"},
                "step_id": {"type": "string"},
                "content": {"type": "string"},
                "parent_prompt_id": {"type": "string"},
                "rationale": {"type": "string"},
                "generator": {
                    "type": "string",
                    "description": "Model/agent that drafted the candidate",
                },
            },
            "required": ["persona", "step_id", "content"],
        },
    ),
    Tool(
        name="meta_conductor_evaluate",
        description=(
            "Evaluate a Meta-Conductor candidate against PRISM's promotion "
            "policy. Required metrics include baseline_score, holdout_score, "
            "contextpack_score, tests_passed, token_ratio, retry_delta, "
            "followup_delta, revert_delta, and sample_n. Passing candidates "
            "are promoted into prompt_variants with source='meta-conductor'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "metrics": {
                    "type": "object",
                    "properties": {
                        "baseline_score": {"type": "number"},
                        "holdout_score": {"type": "number"},
                        "train_score": {"type": "number"},
                        "contextpack_score": {"type": "number"},
                        "tests_passed": {"type": "boolean"},
                        "token_ratio": {"type": "number"},
                        "retry_delta": {"type": "number"},
                        "followup_delta": {"type": "number"},
                        "revert_delta": {"type": "number"},
                        "sample_n": {"type": "integer"},
                    },
                },
            },
            "required": ["candidate_id", "metrics"],
        },
    ),
    Tool(
        name="meta_conductor_auto",
        description=(
            "Run PRISM's deterministic no-LLM Meta-Conductor auto-proposer "
            "for one persona/step. It mines recorded PSP outcome traces and "
            "stores a conservative prompt candidate. If benchmark metrics are "
            "provided, it also applies the normal promotion gate."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "persona": {"type": "string"},
                "step_id": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
                "metrics": {
                    "type": "object",
                    "description": "Optional metrics for immediate gated evaluation",
                },
            },
            "required": ["persona", "step_id"],
        },
    ),
    Tool(
        name="brain_list",
        description="List all documents indexed in Brain. Returns doc_id, domain, and content length for each.",
        inputSchema={
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Filter by domain (code, docs, config, expertise, etc.)"},
                "limit": {"type": "integer", "description": "Max results", "default": 100},
            },
        },
    ),
    Tool(
        name="brain_graph",
        description=(
            "Query the knowledge graph for entity relationships. By "
            "default excludes rationale nodes (kind='rationale') so "
            "graph traversal returns code-flow targets, not "
            "graphify-extracted comment metadata."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entity": {"type": "string",
                           "description": "Entity name to query"},
                "relation": {"type": "string",
                             "description": "Filter by relation type"},
                "limit": {"type": "integer",
                          "description": "Max results", "default": 10},
                "include_rationale": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include rationale nodes (kind='rationale') "
                        "in results. Default false."
                    ),
                },
            },
            "required": ["entity"],
        },
    ),
    Tool(
        name="graph_rebuild",
        description=(
            "Rebuild the code knowledge graph for this project using graphify "
            "(tree-sitter AST pass, Leiden community detection, rationale "
            "extraction). Operates on source files staged via prior "
            "brain_index_doc calls. LLM-free, runs locally. Returns counts of "
            "nodes, edges, communities, and imported entities/relationships."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="prism_status",
        description=(
            "Check whether this project's Brain/Graph layers are in sync. "
            "Returns doc counts, staged-file count, graph stats, and a "
            "`stale` flag with `reasons`. If called with `file_hashes` "
            "({path: sha256}), also returns precise `drifted: [...]` list "
            "with reason `missing` or `content_changed` for each path that "
            "doesn't match Brain. Called by the SessionStart hook."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_hashes": {
                    "type": "object",
                    "description": "Optional {path: sha256} map of on-disk "
                                   "files to diff against Brain's "
                                   "content_hash. Enables precise drift "
                                   "detection.",
                    "additionalProperties": {"type": "string"},
                },
            },
        },
    ),
    Tool(
        name="prism_sync",
        description=(
            "Make Brain + Graph self-consistent. Backfills the graphify "
            "staging dir from any docs in Brain that weren't staged, then "
            "runs graph_rebuild. Idempotent."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="prism_refresh",
        description=(
            "Batch-ingest a map of {path: content}. Blocks until all "
            "chunks are in brain.db and (when skip_graph is false) the "
            "graph has been rebuilt — when this call returns, the files "
            "ARE queryable via brain_search and (unless you skipped) "
            "brain_graph / brain_find_references.\n\n"
            "Set skip_graph=true on every call of a bulk loader except "
            "the last, then call graph_rebuild once at the end. Graph "
            "rebuild walks the whole staging dir per call and dominates "
            "latency (~100s even for one file on a ~100-file project); "
            "amortizing it across a batch is the difference between "
            "'usable bulk ingest' and '30 hours wall-clock'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "files": {
                    "type": "object",
                    "description": "{path: content} map for bulk re-ingest.",
                    "additionalProperties": {"type": "string"},
                },
                "domain": {
                    "type": "string",
                    "description": "Default domain for files without a per-path override. Default 'code'.",
                },
                "skip_graph": {
                    "type": "boolean",
                    "description": "When true, index the files but skip the per-call graph_rebuild. Call graph_rebuild once at the end of a bulk load. Default false.",
                },
            },
            "required": ["files"],
        },
    ),
    Tool(
        name="prism_bulk_refresh",
        description=(
            "Ingest a large {path: content} map with server-side chunking "
            "and automatic graph rebuild at the end. Use this instead of "
            "rolling chunking on the client: callers stop needing to "
            "tune chunk_size to the server's behavior.\n\n"
            "Semantics: splits files into batches of `chunk_size` "
            "(default 25), indexes each batch with skip_graph=true, "
            "runs graph_rebuild once at the end unless skip_graph is "
            "set. Blocks until complete, same contract as prism_refresh. "
            "Supports cancellation via prism_cancel_pending.\n\n"
            "Backpressure: when `PRISM_MAX_CONCURRENT_REFRESH` other "
            "refreshes are in flight (default 2), returns "
            "{busy: true, in_flight: N, retry_after_s: 30} instead of "
            "queuing. Clients should back off rather than pile more work "
            "onto a saturated server."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "files": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "domain": {"type": "string"},
                "chunk_size": {"type": "integer",
                                 "description": "default 25"},
                "skip_graph": {"type": "boolean",
                                "description": "skip final graph_rebuild; default false"},
            },
            "required": ["files"],
        },
    ),
    Tool(
        name="prism_cancel_pending",
        description=(
            "Request cancellation of an in-flight prism_refresh for the "
            "current project. The request is consumed at the next "
            "unit-of-work boundary inside the refresh loop (between "
            "files) — files that have already been indexed stay "
            "indexed, the remaining batch is skipped, and the graph "
            "rebuild is skipped. Returns {cancelled_requested: bool}. "
            "One cancel per one refresh; subsequent refreshes start "
            "clean. Use together with prism_status.indexing_in_flight "
            "to confirm the refresh actually ended."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="prism_install",
        description=(
            "Return the install manifest a coding agent should apply to "
            "the current project on first onboard: files to create "
            "(.claude/settings.json hooks block + hook scripts), step-by-step "
            "instructions, and verification steps. The MCP is self-describing "
            "— no external docs needed. Call this inside project_onboard's "
            "flow or any time you want to re-install the client-side hooks. "
            "Pass `host_platform` (sys.platform of the host running Claude "
            "Code) so hook commands use the right Python launcher: "
            "`python3` on Linux/macOS, `py -3` on Windows. Default is "
            "POSIX/`python3`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host_platform": {
                    "type": "string",
                    "description": (
                        "Host OS where the hooks will run. Accepts "
                        "`sys.platform` values (`linux`, `darwin`, `win32`) "
                        "or aliases (`windows`, `macos`, `posix`). Determines "
                        "whether hook commands use `python3` (POSIX) or "
                        "`py -3` (Windows, via the PEP 397 launcher)."
                    ),
                },
            },
        },
    ),
    Tool(
        name="prism_guide",
        description=(
            "READ FIRST. Returns a concise orientation for this PRISM instance: "
            "what each tool does, when to use it, the daily workflow loop, and "
            "common anti-patterns. Call this once at session start if you're a "
            "coding agent that hasn't used PRISM in this project before."
        ),
        inputSchema={"type": "object", "properties": {
            "section": {"type": "string", "description":
                "Optional: 'overview' | 'tools' | 'workflow' | 'memory' | "
                "'graph' | 'examples'. Omit for the full guide."},
        }},
    ),
    Tool(
        name="memory_store",
        description=(
            "Store an expertise entry in long-term memory. IMPORTANT: Always include "
            "file paths, code examples, and specific details in the description — "
            "a memory entry without evidence is nearly useless. If this fact supersedes "
            "an older one, the old entry is automatically invalidated (not deleted)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Expertise domain: conventions, architecture, testing, billing, deployment, etc.",
                },
                "name": {
                    "type": "string",
                    "description": "Short kebab-case name (e.g. 'two-record-model', 'jwt-refresh-flow')",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "DETAILED description with file paths and code examples. "
                        "BAD: 'Use minimal APIs'. "
                        "GOOD: 'All endpoints use Minimal APIs with TypedResults (not controllers). "
                        "Routes defined in Features/*/Endpoints.cs, each delegating to a Handler.cs. "
                        "Example: Features/Matches/MatchesEndpoints.cs maps GET /api/matches to GetMatchesHandler.'"
                    ),
                },
                "type": {
                    "type": "string",
                    "description": "pattern (reusable code pattern), convention (project rule), failure (bug/incident), decision (architectural choice)",
                },
                "classification": {
                    "type": "string",
                    "description": "tactical (short-term), foundational (core to project), strategic (long-term direction)",
                },
                "evidence": {
                    "type": "object",
                    "description": "Supporting evidence: {file_paths: [...], commit: '...', pr: '...'}",
                },
                "importance": {
                    "type": "integer",
                    "description": "1-10 importance score. 10=critical project knowledge, 5=useful, 1=trivia. Default 5.",
                    "default": 5,
                },
                "memory_type": {
                    "type": "string",
                    "description": "semantic (fact/convention), episodic (specific incident/debug session), procedural (how-to/template). Default: semantic.",
                    "default": "semantic",
                },
                "adr_status": {
                    "type": "string",
                    "description": (
                        "ADR lifecycle status for type=decision memories: "
                        "proposed | accepted | superseded. Leave empty for "
                        "non-ADR entries."
                    ),
                },
                "supersedes": {
                    "type": "string",
                    "description": (
                        "mx-XXXXXX id of the decision/ADR memory this entry "
                        "supersedes (ADR structure, queryable via memory "
                        "tools)."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Optional. When provided, stamps the memory_meta "
                        "sidecar row with this session so the janitor can "
                        "tie memories back to the session that wrote them."
                    ),
                },
            },
            "required": ["domain", "name", "description", "type", "classification"],
        },
    ),
    Tool(
        name="memory_recall",
        description=(
            "Search long-term memory using full-text search. Supports natural language "
            "queries — not just keywords. Returns active, temporally valid entries sorted by importance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for expertise recall"},
                "domain": {"type": "string", "description": "Filter by domain"},
                "limit": {"type": "integer", "description": "Max results", "default": 5},
            },
            "required": ["query"],
        },
    ),
    # ------------------------------------------------------------------
    # OKF — browse PRISM's stores as a live, read-only OKF wiki. Both
    # tools are pure projections (never write brain.db / graph.db).
    # ------------------------------------------------------------------
    Tool(
        name="okf_index",
        description=(
            "Browse the Understand wiki: the OKF manifest of this project's "
            "knowledge (curated memory as navigable concepts). Returns "
            "okf_version, the top-level sections, the concept count, and every "
            "concept path. Read-only projection (never writes brain/graph db)."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="okf_get",
        description=(
            "Read one concept from the Understand wiki by path "
            "(e.g. '/memory/<domain>/<name>.md'): its type, frontmatter, "
            "markdown body, cross-links, and inbound backlinks. Paths come "
            "from okf_index / okf_graph."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Concept path from okf_index"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="principles_seed",
        description=(
            "Seed this project's architecture PRINCIPLES (machine-checkable "
            "layer rules) as memory data so the conductor's plan_gate is "
            "satisfiable on a fresh project (issue #171). With no args it "
            "seeds a generic, repo-agnostic default set (domain !-> "
            "infrastructure; interface !-> domain); pass `rules` to seed a "
            "tailored set. Idempotent (same-name supersedes). Returns the "
            "seeded count + ids."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "rules": {
                    "type": "array",
                    "description": (
                        "Optional custom principles. Each: {id, kind: "
                        "'layer_rule', from, must_not_depend_on, why?}."),
                    "items": {"type": "object"},
                },
            },
        },
    ),
    Tool(
        name="prism_onboard",
        description=(
            "BOOTSTRAP entry for a fresh agent landing on a project. In one "
            "call it (a) SEEDS the generic default architecture principles "
            "(same path as principles_seed) so the conductor's plan_gate is "
            "immediately satisfiable, and (b) returns a bootstrap payload: "
            "the .mcp.json snippet (streamable-HTTP url /mcp/?project=<slug>), "
            "the web + MCP ports, the running PRISM version, a 'call "
            "prism_guide first' pointer to the max-fan-out playbook, the "
            "tool_profile=all url for maintenance/legacy endpoints, and a "
            "`staying_current` block telling the agent how to RECONNECT to pick "
            "up new MCP endpoints after a PRISM upgrade. Call this ONCE on a new "
            "project, then call prism_guide. Idempotent — re-run after a PRISM "
            "version change to refresh the snippet + self-update steps."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="okf_graph",
        description=(
            "Return the Understand wiki's concept GRAPH for this project — "
            "nodes (concepts: id, title, type, domain) + edges (cross-links). "
            "The agent-facing view of the unified Understand wiki."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # ------------------------------------------------------------------
    # LL-08 — Janitor / Layer-B queue endpoints. PRISM schedules the
    # work; the caller's Claude does the LLM compute via the prism-
    # reflect sub-agent. See services/janitor_service.py for the
    # underlying semantics.
    # ------------------------------------------------------------------
    Tool(
        name="janitor_enqueue",
        description=(
            "Enqueue a consolidation candidate. Idempotent on "
            "(task_id, trigger) within a 10-min window. Fire-and-forget "
            "from the Stop hook."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "session_id": {"type": "string"},
                "trigger": {"type": "string",
                             "description": "e.g. session_end, task_done, revert_detected, staleness_sweep"},
                "scope": {
                    "type": "object",
                    "description": "{task_ids, memory_ids, file_paths} — what the session touched",
                },
            },
            "required": ["trigger"],
        },
    ),
    Tool(
        name="janitor_mark_stale",
        description=(
            "Flip pending candidates whose scope overlaps the session's "
            "activity to status=stale and requeue fresh siblings. Called "
            "by the Stop hook so the next reflection sees current state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "scope": {
                    "type": "object",
                    "description": "{task_ids, memory_ids, file_paths} session touched",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="janitor_check",
        description=(
            "Return {ready, brief}. Dispenses at most one pending "
            "candidate per call — if ready, the brief is a subagent "
            "work packet (question, context, mcps_available, "
            "investigation_guidance, response_schema). Enforces the 1h "
            "min queue age and 5-min abandon backoff."
        ),
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="janitor_submit",
        description=(
            "Post the sub-agent's JSON output. Server validates the "
            "response schema, writes consolidation_runs, enriches "
            "task_quality_rollup.qualitative_score. Malformed → reject."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "output_json": {"type": "object"},
            },
            "required": ["candidate_id", "output_json"],
        },
    ),
    Tool(
        name="janitor_abandon",
        description=(
            "Give up on a dispensed candidate. Increments retry_count; "
            "hard limit of 3 before status=abandoned."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["candidate_id"],
        },
    ),
    Tool(
        name="janitor_status",
        description=(
            "Return queue depth by status + last-nudged timestamps. Used "
            "by the /consolidation UI and by operators debugging why "
            "nothing is dispensing."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # ------------------------------------------------------------------
    # Ultimate Graph annotation PULL loop (#50). DISTINCT from janitor_*:
    # response schema is {name, purpose}; durable in graph.db graph_jobs.
    # See services/graph_annotate.py.
    # ------------------------------------------------------------------
    Tool(
        name="graph_annotate_enqueue",
        description=(
            "Sweep the graph's hierarchy + community scopes and enqueue "
            "annotation briefs for scopes whose input_hash changed "
            "(escape-when-unchanged). Idempotent on (scope_kind, scope_id, "
            "input_hash). Returns {enqueued}."
        ),
        inputSchema={
            "type": "object",
            "properties": {"project": {"type": "string"}},
        },
    ),
    Tool(
        name="graph_annotate_check",
        description=(
            "Return {ready, brief}. Dispenses at most one pending "
            "annotation brief per call - the brief carries the rendered "
            "graph_enrich prompt and a response_schema constrained to "
            "{name, purpose}. Distinct from janitor_check (reflection)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="graph_annotate_submit",
        description=(
            "Post the session's inferred {name, purpose}. Server "
            "schema-validates, persists via upsert_annotation with "
            "provenance 'claude @ <date>', marks the job completed. "
            "Malformed output is rejected and the brief stays dispensable."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "brief_id": {"type": "string"},
                "output": {"type": "object"},
            },
            "required": ["brief_id", "output"],
        },
    ),
    Tool(
        name="graph_annotate_abandon",
        description=(
            "Give up on a dispensed annotation brief. Increments retries; "
            "hard limit of 3 before status=abandoned."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "brief_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["brief_id"],
        },
    ),
    Tool(
        name="graph_annotate_status",
        description=(
            "Return annotation-queue depth by status "
            "(pending/dispensed/completed/abandoned)."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="memory_invalidate",
        description=(
            "Soft-delete a memory by flipping its memory_meta row to "
            "status=invalidated. Row is preserved for audit; the JSONL "
            "content stays where it is. Called by the prism-reflect "
            "sub-agent when a reflection determines a memory no longer "
            "applies."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="task_create",
        description="Create a new task in the PRISM task tracker",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "description": {"type": "string", "description": "Task description"},
                "priority": {"type": "integer", "description": "Priority (higher = more important)", "default": 0},
                "dependencies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of task IDs this task depends on",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization",
                },
                "story_file": {"type": "string", "description": "Associated story file path"},
                "assigned_agent": {"type": "string", "description": "Agent persona to assign (sm, dev, qa)"},
                "parent_id": {"type": "string", "description": "Parent task ID — makes this a child (subtask) of an epic. Children are hidden from the /tasks board and reached via the parent's detail page."},
                "oracle": {"type": "string", "description": "The observable signal that proves the user outcome is actually met (the 'oracle' — defined before work starts). E.g. a test suite, demo, artifact, metric, review, or decision."},
                "proof_type": {"type": "string", "description": "Kind of completion evidence: test|demo|artifact|metric|review|source_backed_answer|decision."},
                "completion_proof": {"type": "string", "description": "Receipt-backed evidence the oracle is satisfied; recorded when done and checked (advisory) at green_gate."},
                "likely_misfire": {"type": "string", "description": "How this task could pass-but-be-WRONG (the goalbuddy 'misfire' — recorded upfront and audited advisory at green_gate when completion_proof doesn't address it). The cheapest defense against false-greens."},
                "full_outcome_complete": {"type": "boolean", "description": "goalbuddy GAP-4: True only when the owner's FULL outcome is met (slice green + no incomplete children + strong proof), not just a green slice. Conductor sets this at green_gate; defaults False."},
                "allowed_files": {"type": "array", "items": {"type": "string"}, "description": "Worker contract: the file allowlist this slice may touch. Parallel workers are safe only with disjoint allowlists."},
                "verify": {"type": "array", "items": {"type": "string"}, "description": "Worker contract: commands that prove the slice (e.g. the test command)."},
                "stop_if": {"type": "array", "items": {"type": "string"}, "description": "Worker contract: conditions that HALT the slice (need files outside allowed_files, behavior ambiguous, verification fails twice)."},
                "plan_doc": {"type": "string", "description": "Proposed-change plan as markdown — rendered below the diagram in the PRISM task Plan card."},
                "plan_diagram": {"type": "string", "description": "Mermaid source (sequence/UML) for the plan — rendered at the top of the PRISM task Plan card."},
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="task_list",
        description="List tasks with optional filters",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: pending, in_progress, done, blocked",
                },
                "assigned_agent": {"type": "string", "description": "Filter by assigned agent"},
                "tag": {"type": "string", "description": "Filter by tag"},
                "story_file": {"type": "string", "description": "Filter by story file"},
                "parent_id": {"type": "string", "description": "Scope to ONE epic's children — pass an epic id for its direct children, or '' for root tasks only (FR-6)."},
                "fields": {"type": "array", "items": {"type": "string"}, "description": "Projection: return only these keys per task (lean response, FR-7). Omit for the full task rows."},
            },
        },
    ),
    Tool(
        name="task_next",
        description="Get the next highest-priority unblocked task to work on",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="task_update",
        description="Update an existing task (status, priority, assignment, etc.)",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Task ID to update"},
                "title": {"type": "string", "description": "Rename the task. Blank/whitespace is ignored (never blanks an existing title)."},
                "status": {
                    "type": "string",
                    "description": "New status: pending, in_progress, done, blocked",
                },
                "priority": {"type": "integer", "description": "New priority"},
                "assigned_agent": {"type": "string", "description": "New agent assignment"},
                "blocked_reason": {"type": "string", "description": "Reason for blocking (when status=blocked)"},
                "parent_id": {"type": "string", "description": "Re-parent this task under an epic (or '' to make it a root)."},
                "oracle": {"type": "string", "description": "Set/replace the oracle — the observable signal that proves the outcome."},
                "proof_type": {"type": "string", "description": "test|demo|artifact|metric|review|source_backed_answer|decision."},
                "completion_proof": {"type": "string", "description": "Receipt-backed evidence the oracle is satisfied (checked advisory at green_gate)."},
                "likely_misfire": {"type": "string", "description": "How this task could pass-but-be-WRONG (audited advisory at green_gate)."},
                "full_outcome_complete": {"type": "boolean", "description": "goalbuddy GAP-4: owner's FULL outcome met (not just a green slice). Conductor sets this at green_gate; defaults False."},
                "allowed_files": {"type": "array", "items": {"type": "string"}, "description": "Worker contract: set the file allowlist for the slice."},
                "verify": {"type": "array", "items": {"type": "string"}, "description": "Worker contract: set the verify commands."},
                "stop_if": {"type": "array", "items": {"type": "string"}, "description": "Worker contract: set the stop conditions."},
                "plan_doc": {"type": "string", "description": "Proposed-change plan as markdown — rendered below the diagram in the PRISM task Plan card."},
                "plan_diagram": {"type": "string", "description": "Mermaid source (sequence/UML) for the plan — rendered at the top of the PRISM task Plan card."},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="task_link_session",
        description=(
            "Force-link a Claude session to a task — upserts a "
            "task_sessions(task_id, session_id) row so per-task session "
            "history/metrics surface on the task view. session_id is "
            "caller-passed; when omitted the active request session is "
            "used. Rides the task_* family (single route), not a "
            "parallel surface."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to link"},
                "session_id": {
                    "type": "string",
                    "description": (
                        "Session to link. Optional — defaults to the "
                        "active request session when omitted."
                    ),
                },
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="workflow_state",
        description="Get the current PRISM workflow state (active step, progress, session info)",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="workflow_advance",
        description=(
            "Deprecated: prefer `conductor_advance(id=...)` for per-task "
            "state. This tool drives the legacy session-global "
            "WorkflowState and does not update the per-task "
            "workflow_step / gate_state set by Conductor v2."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "validation": {"type": "string", "description": "Validation result to record for current step"},
                "gate_action": {
                    "type": "string",
                    "description": "Action for gate steps: approve or reject",
                },
            },
        },
    ),
    Tool(
        name="conductor_advance",
        description=(
            "Advance a task to the next entry in WORKFLOW_STEPS "
            "(Conductor v2 per-task state machine). Refuses if the task "
            "is currently sitting on a gate with gate_state='pending' — "
            "call `conductor_gate` first. Returns `{ok, task_id, "
            "from_step, to_step, gate_state, task}` where `task` is the "
            "updated task row."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Task ID to advance"},
                "validation": {
                    "type": "string",
                    "description": "Optional validation note recorded on the transition history row",
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Optional session to associate with this task on "
                        "advance (auto-writer into task_sessions)."
                    ),
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Projection (FR-7): return ONLY these keys (e.g. "
                        "['from_step','to_step','gate_state']) and OMIT the "
                        "full task object — a lean response. Omit to get the "
                        "full {ok, task_id, from_step, to_step, gate_state, "
                        "task} (+ rubric on authoring steps)."
                    ),
                },
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="conductor_gate",
        description=(
            "Resolve a gate on a task (Conductor v2 / per-task SDLC "
            "state machine). REASON IS REQUIRED on every approve and "
            "describes the validation evidence the caller used to "
            "satisfy the gate (test run, screenshot path, manual "
            "review notes, etc.). The reason is persisted to "
            "task.gate_reason on pass so it surfaces on the kanban "
            "and /conductor swimlanes. action='approve' (default "
            "path): consults VerifierService against the prior-step "
            "validation if one is wired; releases the gate when "
            "verifier confirms, else flips to 'failed' with the "
            "verifier's reason. action='reject' flips to 'failed' "
            "and stores reason. Set override=true to bypass the "
            "verifier (force pass) and/or recover from gate_state="
            "'failed'; audited as actor='manual-override'. "
            "Returns {ok, task_id, gate_step, gate_state, to_step?, "
            "auto_advanced?, verifier?, validation?, override?, task}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Task ID whose gate to decide"},
                "action": {
                    "type": "string",
                    "enum": ["approve", "reject"],
                    "description": "Gate decision: approve or reject",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Validation evidence describing how the caller "
                        "satisfied the gate (test run, screenshot path, "
                        "manual review notes, etc.). REQUIRED on every "
                        "approve; recorded to task.gate_reason on pass "
                        "and visible in task_history and on the /conductor "
                        "swimlanes. Different for each task."
                    ),
                },
                "override": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Bypass VerifierService (force pass) and/or recover "
                        "from gate_state='failed'. Audited as actor="
                        "'manual-override'. story_gate/plan_gate are "
                        "RUBRIC-VERIFIED (pure YAML-rubric functions over "
                        "the task's plan_doc/plan_diagram) — a compliant "
                        "drive needs NO override there; override remains "
                        "only for the terminal green_gate (no machine-"
                        "sensible test). NO SELF-OVERRIDE: an override by "
                        "the SAME actor that produced the work is rejected "
                        "— an independent verifier (distinct actor) must "
                        "re-run the claimed command. Override also cannot "
                        "bypass the proof-carrying artifact requirement "
                        "(red_gate needs a committed failing-test trace; "
                        "green_gate a captured full-suite-green)."
                    ),
                },
                "actor": {
                    "type": "string",
                    "description": (
                        "Who is clearing the gate. On an override, this "
                        "must be DISTINCT from the actor(s) that produced "
                        "the work (the sessions linked to the task) — a "
                        "same-actor self-override is rejected."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": "Active Claude session id (links task<->session).",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Projection (FR-7): return ONLY these keys (e.g. "
                        "['gate_step','gate_state','to_step']) and OMIT the "
                        "full task object — a lean response. Omit to get the "
                        "full decision payload plus the task row."
                    ),
                },
            },
            "required": ["id", "action", "reason"],
        },
    ),
    Tool(
        name="context_bundle",
        description=(
            "Build a deterministic MCP-side context bundle: role card, rules, "
            "template, Brain context, memory recall, active tasks, workflow "
            "state, and health. Existing top-level fields are preserved for "
            "compatibility; new clients should prefer context_pack."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "persona": {"type": "string", "description": "Agent persona (sm, dev, qa) for context filtering"},
                "story_file": {"type": "string", "description": "Story file path for scoped context"},
            },
        },
    ),
    Tool(
        name="project_list",
        description="List all PRISM projects with data in this service",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="project_create",
        description="Create a new isolated PRISM project",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project identifier (slug, e.g. 'my-app')",
                },
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="project_onboard",
        description=(
            "Onboard PRISM into a project. Returns a structured onboarding checklist "
            "that the Architect persona should work through. Claude reads the project "
            "files on the host, analyzes them, and stores findings via memory_store "
            "and brain_index_doc. A PRISM project can span multiple repos/directories."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Human-readable project name",
                },
                "sub_projects": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Sub-project name (e.g. 'api-server', 'web-client')"},
                            "path": {"type": "string", "description": "Root path on host filesystem"},
                            "tech": {"type": "string", "description": "Primary tech stack (e.g. '.NET 9', 'React + TypeScript')"},
                        },
                    },
                    "description": "The sub-projects/repos that make up this PRISM project",
                },
                "conventions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Known project conventions to seed immediately",
                },
                "host_platform": {
                    "type": "string",
                    "description": (
                        "Host OS where Claude Code (and therefore the "
                        "PRISM hooks) will run. Accepts sys.platform "
                        "values (`linux`, `darwin`, `win32`) or aliases "
                        "(`windows`, `macos`, `posix`). Forwarded to "
                        "prism_install so hook commands use `python3` on "
                        "POSIX and `py -3` on Windows."
                    ),
                },
            },
            "required": ["project_name"],
        },
    ),
    # ------------------------------------------------------------------
    # Verifier — outer-harness sensor (Tier 0 tooling, Tier 1 records)
    # ------------------------------------------------------------------
    Tool(
        name="verifier_run",
        description=(
            "Run the outer-harness verifier over the current project. "
            "Tier 0 invokes the project's own tooling (ruff/mypy/pytest "
            "for Python, eslint/tsc for JS/TS, cargo check for Rust, go "
            "vet for Go) scoped to git-diff'd files. Tier 1 walks PRISM "
            "tables (brain_index_doc claims, tasks marked done, memory "
            "writes) since session start and confirms each claim against "
            "current state. Returns a structured verdict per claim plus a "
            "top-line status (pass | fail | partial | error). Designed to "
            "fire from the Stop hook on every session — typical run is "
            "<1s when no diff is in scope."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string",
                    "description": "Claude Code session id; scopes Tier 1 claim collection."},
                "task_id": {"type": "string",
                    "description": "Optional task id this run is associated with."},
                "since_iso": {"type": "string",
                    "description": "Override start-of-window timestamp; default = "
                                   "session_outcomes.timestamp or 1h ago."},
                "baseline_rev": {"type": "string",
                    "description": "Git revision to diff against for Tier 0 scope; "
                                   "default = HEAD."},
                "workspace": {"type": "string",
                    "description": "Host project directory (${CLAUDE_PROJECT_DIR}). "
                                   "Required so Tier 0 runs against the real source "
                                   "tree, not the MCP container's cwd."},
            },
        },
    ),
    Tool(
        name="verifier_history",
        description=(
            "Recent verifier runs, newest first. Filter by task_id to get "
            "the per-task flywheel — what got verified, what failed, "
            "trends in tier statuses over time. Used by the dashboard "
            "and by agents inspecting their own history before reprompting."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="verifier_feedback_summary",
        description=(
            "Unresolved improvement seeds from recent verifier runs — "
            "the human-readable feedback strings the verifier emitted "
            "for fail/partial claims. Surfaced as additionalContext by "
            "the SessionStart hook so the agent picks up where the last "
            "run left off."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
]


# ---------------------------------------------------------------------------
# Tool profiles
# ---------------------------------------------------------------------------

INTERACTIVE_TOOL_NAMES: set[str] = {
    "brain_search",
    "brain_understand",
    "brain_find_symbol",
    "brain_outline",
    "brain_find_references",
    "brain_call_chain",
    "prism_status",
    "prism_sync",
    "prism_guide",
    "memory_store",
    "memory_recall",
    "okf_index",
    "okf_get",
    "okf_graph",
    "principles_seed",
    "prism_onboard",
    "task_create",
    "task_list",
    "task_next",
    "task_update",
    "task_link_session",
    "workflow_state",
    "workflow_advance",
    "conductor_advance",
    "conductor_gate",
    "context_bundle",
    "register_claude_source",
    # GH #173 — the prism-reflect sub-agent connects through this default
    # profile; SessionStart advertises PRISM_REFLECTION_PENDING and the
    # agent spec's allow-list calls these. Serve them so candidates are
    # actionable (fetch brief -> submit/abandon verdict, invalidate stale).
    "janitor_check",
    "janitor_submit",
    "janitor_abandon",
    "memory_invalidate",
}
# NOTE: the legacy understand_* tools are intentionally NOT in the default
# interactive surface — they're superseded by the okf_* Understand wiki and
# kept reachable only via tool_profile=all (plus understand_refresh/status in
# the automation profile for the stop hook). This keeps the default agent
# surface a small curated subset, per the tool-surface-reduction objective
# (benchmarks/objective_audit). Reachable via tool_profile=all when needed.

# Splice the understand_* tools into the registration list so the
# MCP server advertises them alongside the original surface.
TOOLS.extend(UNDERSTAND_TOOLS)

ADMIN_TOOL_NAMES: set[str] = {
    "project_list",
    "project_create",
    "project_onboard",
    "prism_install",
    "prism_refresh",
    "prism_bulk_refresh",
    "prism_cancel_pending",
    "brain_index_doc",
    "brain_list",
    "brain_graph",
    "graph_rebuild",
    "verifier_run",
    "verifier_history",
}

HOOK_TOOL_NAMES: set[str] = {
    "record_session_outcome",
    "record_skill_usage",
    "record_outcome",
    "record_subagent_outcome",
    "verifier_feedback_summary",
}

LEARNING_TOOL_NAMES: set[str] = {
    "brain_search_feedback",
    "meta_conductor_brief",
    "meta_conductor_propose",
    "meta_conductor_evaluate",
    "meta_conductor_auto",
    "janitor_enqueue",
    "janitor_mark_stale",
    "janitor_check",
    "janitor_submit",
    "janitor_abandon",
    "janitor_status",
    "graph_annotate_enqueue",
    "graph_annotate_check",
    "graph_annotate_submit",
    "graph_annotate_abandon",
    "graph_annotate_status",
    "memory_invalidate",
}

AUTOMATION_TOOL_NAMES: set[str] = {
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
    "graph_annotate_enqueue",
    "graph_annotate_check",
    "verifier_run",
    # GH #99 part 3: the Stop hook fires these read/refresh nudges over
    # the automation profile (stop_record_hook.py understand_refresh +
    # understand_status). Granting them here keeps those calls from being
    # silently rejected (now isError=true) drops. Least-privilege
    # record_* split is unchanged.
    "understand_refresh",
    "understand_status",
}

TOOL_PROFILE_ALIASES: dict[str, str] = {
    "all": "all",
    "default": "interactive",
    "core": "interactive",
    "interactive": "interactive",
    "admin": "admin",
    "project": "admin",
    "hooks": "hooks",
    "telemetry": "hooks",
    "learning": "learning",
    "automation": "automation",
    "hooks_api": "automation",
}


def tool_names_for_profile(profile: str | None) -> set[str]:
    """Return MCP tool names for a public profile name."""
    profile_key = TOOL_PROFILE_ALIASES.get(
        (profile or "interactive").strip().lower(),
        "interactive",
    )
    all_names = {tool.name for tool in TOOLS}
    if profile_key == "interactive":
        return INTERACTIVE_TOOL_NAMES & all_names
    if profile_key == "admin":
        return ADMIN_TOOL_NAMES & all_names
    if profile_key == "hooks":
        return HOOK_TOOL_NAMES & all_names
    if profile_key == "learning":
        return LEARNING_TOOL_NAMES & all_names
    if profile_key == "automation":
        return AUTOMATION_TOOL_NAMES & all_names
    return all_names


def tools_for_profile(profile: str | None) -> list[Tool]:
    """Return MCP tool definitions visible for a profile."""
    allowed = tool_names_for_profile(profile)
    return [tool for tool in TOOLS if tool.name in allowed]


# ---------------------------------------------------------------------------
# Convention enrichment
# ---------------------------------------------------------------------------

def _enrich_convention(text: str) -> str:
    """Expand a short convention one-liner into a richer description.

    Ensures the description contains explicit "Never"/"Do not" phrasing
    so the pre-write-convention-guard hook can extract anti-patterns.
    """
    lower = text.lower()
    # Already has enforcement language — return as-is
    if any(kw in lower for kw in ("never", "do not", "don't", "avoid", "prohibited")):
        return text
    # Expand common convention patterns into enforceable descriptions
    if "must use" in lower or "must be" in lower:
        return f"{text}. Do not deviate from this convention."
    if "no " in lower and ("prefix" in lower or "suffix" in lower):
        return f"{text}. Never use alternative naming formats."
    return f"{text}. Do not violate this convention."


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def _serialise(obj: Any) -> Any:
    """Convert dataclasses and other non-JSON types for serialisation."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, list):
        return [_serialise(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    return obj


def _json(obj: Any) -> str:
    """Serialise *obj* to a JSON string, handling dataclasses."""
    return json.dumps(_serialise(obj), indent=2, default=str)


def _resolve_link_session_id() -> str:
    """Fallback session id to stamp into task_sessions when a conductor
    advance/gate omits one. Prefers the REAL active transcript session for
    the request's project (so the link maps to actual token data); falls
    back to the MCP request handle only when no transcript can be found —
    that preserves the "always stamp something" guarantee without making a
    phantom id the default (which left conductor tiles stuck at 0 tok)."""
    from prism_service.mcp.request_context import get_request_context
    ctx = get_request_context()
    try:
        from prism_service.services.claude_transcripts import (
            _project_source_path, current_session_id,
        )
        from prism_service.services import claude_memory
        src = _project_source_path(ctx.project_id)
        # Resolve the explicit claude_project_dir so folder-mode / cwd-mismatch
        # (#134) — where src='' — still stamps a REAL session id instead of the
        # phantom MCP request handle (which maps to no transcript).
        override_dir = claude_memory.configured_project_dir(ctx.project_id) or ""
        if src or override_dir:
            real = current_session_id(src, override_dir=override_dir)
            if real:
                return real
    except Exception:
        pass
    return ctx.request_id


# ---------------------------------------------------------------------------
# Self-documenting guide (returned by prism_guide tool)
# ---------------------------------------------------------------------------

def _version_banner() -> str:
    try:
        from prism_service.__version__ import PRISM_VERSION as _v, PRISM_VERSION_NOTES as _n
        return f"PRISM version: **{_v}** — {_n}"
    except Exception:
        return "PRISM version: unknown"


_GUIDE_SECTIONS: dict[str, str] = {
    "overview": _version_banner() + "\n\n" + """\
# PRISM — what it is

An on-prem memory + knowledge layer for coding agents. Knowledge lives in TWO
surfaces (plus Tasks + Workflow), all accessed via this MCP endpoint:

- **Brain** — the code-graph VISUALIZATION (Sigma WebGL) plus retrieval over
  source files, docs, and architecture notes. Retrieve with `brain_search`
  (hybrid BM25 + dense vector + graph RRF) and `brain_understand`; the graph
  itself is graphify (tree-sitter + Leiden clustering).
- **Understand** — ONE unified wiki over the project's curated MEMORY,
  projected as navigable OKF concepts: a concept graph, readable concept
  bodies, cross-links, and backlinks. Browse it with `okf_index` /
  `okf_get` / `okf_graph`. Memory is what you READ here; you still WRITE it
  with `memory_store` and search the raw entries with `memory_recall`.
- **Tasks** — kanban-style tracker with dependencies, priorities, personas.
- **Workflow** — SDLC state machine (planning → RED → GREEN → review) with
  per-step gates.

Everything is scoped per project via `?project=<slug>` on this URL. Data lives
in SQLite inside the container's /data volume — no network, no API keys.

# First contact — do this at session start if the project isn't initialized

1. `project_list` — is this project already onboarded?
2. `prism_status` — are Brain and Graph in sync? Returns `stale: true` with
   concrete reasons if anything drifted (e.g. docs ingested before graphify
   was wired up, or schema migrations not applied). **If stale, call
   `prism_sync` to self-heal.**
3. If project is not onboarded: `project_onboard(project_name="...",
   sub_projects=[...])` returns a 7-step Architect checklist. Walk it:
   discover structure, identify tech stack, map entry points, discover
   conventions, index key files via `brain_index_doc`, call
   `graph_rebuild` once after the batch, store initial conventions via
   `memory_store`.
4. If already onboarded and in sync: `context_bundle(persona="dev")` to
   load the deterministic role card/rules/template packet plus current
   tasks, workflow, Brain context, and recent memory.

## Keeping in sync as you go

- Every `brain_index_doc` with a code-suffix path (.py/.ts/.js/.cs/.go/etc)
  auto-stages the file for graphify. You do NOT need to re-stage manually.
- After a BATCH of ingests, call `graph_rebuild` once. Don't call per file.
- `graph_rebuild` auto-backfills from the Brain docs table if staging is
  empty — so you can't get "graph frozen behind Brain" for long.
- If `prism_status` reports staleness, `prism_sync` fixes it in one call
  (backfill + rebuild).

Only call `prism_guide` (this tool) once per session — the guide doesn't
change between calls. Cache it.
""",
    "tools": """\
# All tools — what they do and when to call them

## Project lifecycle + sync health (CALL THESE FIRST)
- `project_list()` — list all projects with data in this instance. Check if
  the current slug is already onboarded.
- `project_create(project_id)` — create a new isolated project. Rarely needed
  manually; the service auto-creates on first MCP hit with a new slug.
- **`project_onboard(project_name, sub_projects?, conventions?)`** — THE
  initialization route. Returns a 7-step checklist. If you're a fresh
  coding agent in a brand-new PRISM project, call this BEFORE doing
  anything else. It seeds project identity + sub-project map into memory
  so later sessions know the layout.
- **`prism_status()`** — sync health check. Returns doc counts, staged-file
  count, graph entity/relationship count, graphify coverage, and a list of
  staleness reasons. Call at session start.
- **`prism_sync()`** — idempotent self-heal. Backfills graphify staging
  from the Brain docs table, then runs `graph_rebuild`. Use when
  `prism_status` reports `stale: true`.

## Brain (indexed knowledge)
- `brain_index_doc(path, content, domain)` — **you read the file on the
  host and send the content here**. PRISM indexes into FTS + vector + stages
  for the code graph. Call for every source file you want searchable.
- `brain_search(query, limit, domain?, domains?)` — hybrid RRF search
  (BM25 + vector + graph). Returns ranked docs with content + rrf_score.
  Default limit 5.
- `brain_list(domain?, limit?)` — list indexed docs. Useful for a sanity
  check after bulk ingest.
- `brain_graph(entity, relation?, limit?)` — query the code graph by
  entity name. Returns related nodes with relation type.
- **`graph_rebuild()`** — run graphify (tree-sitter AST + Leiden clusters).
  Populates entities and relationships with confidence scores and community
  IDs. **Call once at the end of a bulk-ingest batch**, not per file.

## Memory (long-term expertise)
- `memory_store(domain, name, description, type, classification, evidence?,
  importance?, memory_type?)` — save a convention, decision, pattern, or
  failure. Supersession is automatic: if a later entry contradicts an
  older one, the older is marked invalid (not deleted).
  - `type`: pattern | convention | failure | decision
  - `classification`: tactical | foundational | strategic
  - `memory_type`: semantic | episodic | procedural (default: semantic)
  - **description must include file paths and code examples** — vague
    memories are nearly useless.
- `memory_recall(query, domain?, limit?)` — FTS search. Returns active,
  temporally valid entries sorted by importance.

## Understand (the unified wiki over Memory)
Curated memory is browsable as ONE Understand wiki — memory entries projected
as navigable OKF concepts. Read-only; never writes brain.db / graph.db.
- `okf_index()` — the wiki manifest: sections, concept count, every path.
- `okf_get(path)` — read one concept (frontmatter + body + cross-links +
  backlinks).
- `okf_graph()` — the concept GRAPH: nodes (concepts) + cross-link edges.

## Tasks
- `task_create(title, description?, priority?, dependencies?, tags?,
  story_file?, assigned_agent?)` — new task.
- `task_list(status?, assigned_agent?, tag?, story_file?, parent_id?, fields?)`
  — filtered list. `parent_id="<epic>"` scopes to one epic's children (or `""`
  for roots) so a big board doesn't blow the token budget; `fields=[...]`
  projects each row to just those keys.
- `task_next()` — highest-priority unblocked task.
- `task_update(id, status?, priority?, assigned_agent?, blocked_reason?,
  oracle?, proof_type?, completion_proof?)` — mutate. Set `proof_type`
  (test|metric|artifact|demo|…) to pick the gate's oracle shape; `test` is the
  TDD default.
- `conductor_advance(id, validation?, fields?)` /
  `conductor_gate(id, action, reason, fields?, override?)` — drive the per-task
  SDLC. Pass `fields=["from_step","to_step","gate_state"]` for a lean response
  that omits the echoed task object.

## Workflow
- `workflow_state()` — current step, progress, session info.
- `workflow_advance(validation?, gate_action?)` — move to next step. For
  gate steps, pass `gate_action="approve"` or `"reject"`.

## Context + help
- `context_bundle(persona?, story_file?)` — deterministic MCP-side context
  packet. Preserves the legacy brain/memory/tasks/workflow/health fields and
  adds `context_pack` with role card, rules, template, asset digests, and the
  same relevant context nested for model-agnostic clients.
- `prism_guide(section?)` — this tool. Sections: overview | tools |
  workflow | orchestration | memory | graph | examples.
""",
    "workflow": """\
# Daily workflow loop (coding agent)

## Once per project (first session ever)
1. `project_list` — confirm this slug is/isn't already onboarded.
2. `project_onboard(project_name, sub_projects?)` — returns 7-step
   checklist. Walk it: read README/package.json/tsconfig/etc., discover
   tech stack, pick key source files, `brain_index_doc` each, `memory_store`
   each convention you find, then `graph_rebuild` at the end.

## Once per session
1. `prism_guide` (this tool) → cache the result.
2. `context_bundle(persona="dev")` → loads tasks + recent memory + workflow.
3. `workflow_state()` if a workflow is in progress.

## Per task
1. **Gather context** — `brain_search` with the task description. Read top
   3-5 results. For structural questions use `brain_graph(entity=<name>)`.
2. **Recall conventions** — `memory_recall("testing")`, `memory_recall("
   error handling")`, etc. Pick up project-specific rules BEFORE writing.
3. **Write code**.
4. **Learn something** — if you discovered a convention, bug pattern, or
   architectural reason, `memory_store(...)` so future sessions inherit it.
5. **New/changed source files** — `brain_index_doc` each, then
   `graph_rebuild()` once after the batch.
6. **Track progress** — `task_update(status=...)`. `task_next` for the
   next unblocked item.
""",
    "memory": """\
# Memory — the killer feature for coding agents

Agents forget. PRISM's memory is what keeps conventions, decisions, and
incident learnings across sessions.

## When to store
- You just inferred a project convention (e.g. "endpoints use Minimal APIs,
  not controllers"). **Always include the file path where you saw it.**
- A decision was made that would surprise a future agent (e.g. "we chose
  not to use Redis because of compliance").
- A bug was fixed and the root cause is non-obvious.
- A file structure pattern matters (e.g. "handlers live in Features/*/").

## When NOT to store
- Obvious things derivable from `git log` or the codebase itself.
- Task-specific state (use tasks for that).
- Information already in CLAUDE.md — don't duplicate.

## Good vs bad
BAD:  description="Use minimal APIs"
GOOD: description="Endpoints use ASP.NET Minimal APIs with TypedResults
      (not controllers). Routes in Features/*/Endpoints.cs, each delegating
      to a Handler.cs. Example: Features/Matches/MatchesEndpoints.cs maps
      GET /api/matches to GetMatchesHandler.Handle."

Always set `type` (pattern|convention|failure|decision) and
`classification` (tactical|foundational|strategic).
""",
    "graph": """\
# Code graph — what it's for

Brain's graph layer is powered by graphify (tree-sitter + Leiden clustering).
It's populated by calling `graph_rebuild()` after bulk-ingesting source.

## Query patterns
- `brain_graph(entity="MatchesHandler")` — list methods, callers,
  containers of a known class/function.
- Community IDs cluster related entities. Entities in the same community
  are structurally/semantically adjacent.
- Edges have confidence: `EXTRACTED` (tree-sitter direct, conf 1.0),
  `INFERRED` (best-effort), `AMBIGUOUS` (flagged).

## When it helps
- "Who calls X across the repo?" → traverse the graph.
- "What files are in the same module as X?" → check X's community.
- "What's the shape of this class?" → brain_graph returns methods.

## When it doesn't help
- Free-text / conversational queries — those go through vector + BM25.
- Brand-new files not yet in a graph_rebuild batch.
""",
    "examples": """\
# Example flows

## Onboarding a brand-new project (FIRST session)
Requires a maintenance profile such as `?tool_profile=all`.

1. `project_list` → confirm slug unknown.
2. `project_onboard(project_name="My App", sub_projects=[
     {"name": "api", "tech": "C#/.NET", "path": "/home/me/api"},
     {"name": "client", "tech": "React/TS", "path": "/home/me/client"}])`
   → returns a 7-step Architect checklist.
3. Walk it: read README, package.json, pyproject.toml, etc. For every
   important source file, `brain_index_doc(path=<rel>, content=<text>,
   domain="code")`.
4. After the batch: `graph_rebuild()` — builds the code graph in one shot.
5. For each convention you discovered:
   `memory_store(domain="conventions", name="minimal-apis",
    description="Endpoints use Minimal APIs at Features/*/Endpoints.cs...",
    type="convention", classification="foundational",
    evidence={"file_paths": ["src/Features/Matches/MatchesEndpoints.cs"]})`.

## Daily "implement a feature" loop
1. `context_bundle(persona="dev")` → tasks + recent memory.
2. `brain_search("user authentication flow", limit=5)` → relevant files.
3. `memory_recall("auth", limit=5)` → project auth rules.
4. Write code.
5. Let the installed edit-learn hooks ingest changed files; use
   `prism_status()` to check drift and `prism_sync()` if the graph is stale.
6. `task_update(id=..., status="done")`.

## Debugging an incident
1. `memory_recall("similar failure", limit=10)` — seen it before?
2. `brain_search("<error message>", limit=5)` — in any doc?
3. `brain_call_chain(entity="<suspected component>", direction="callers")` —
   who uses it?
4. Fix.
5. `memory_store(type="failure", name="oauth-null-token", description="Root
    cause was X, observed at file.py:123, fix was Y.",
    classification="foundational", importance=8)`.

## Picking up after a crash
1. `workflow_state()` — which step was active?
2. `task_list(status="in_progress")` — what was I doing?
3. `context_bundle()` — full picture.
4. Resume from the last known-good state.
""",
    "orchestration": """\
# Working tasks the PRISM way

PRISM does NOT auto-run your work — YOU (the calling agent) orchestrate the
conductor through the `task_*` / `conductor_*` MCP tools. MAXIMIZE FAN-OUT:
decompose wide, run subtasks IN PARALLEL, verify with a distinct actor.

## 0. Bootstrap once with prism_onboard
On a fresh project call `prism_onboard` FIRST: it seeds the default
architecture principles (so plan_gate is satisfiable), and returns the
.mcp.json snippet + ports + version + a pointer back here. Then `prism_guide`.
STAY CURRENT: your MCP client caches the tool list at connect time, so after a
PRISM upgrade NEW endpoints stay invisible until you RECONNECT — run `/mcp`
(reconnect `prism`) or restart, then re-call `prism_guide`. Compare
`prism_status.prism_version` to the version you onboarded on to detect drift.
Maintenance/legacy endpoints (brain_index_doc, graph_rebuild, the legacy
understand_* family, …) live behind `?tool_profile=all` — reconnect with the
`mcp_url_all` that `prism_onboard` returns to use them.

## 1. Frame an EPIC as a ROOT task, decompose into demonstrable subtasks
- `task_create(title=..., parent_id="")` — a ROOT task (empty parent_id) is an
  EPIC tracked LIVE on /conductor. A task you want to WATCH animate on the
  conductor MUST be root; child tasks are hidden from the tiles.
- Break the epic into demonstrable-FEATURE subtasks with
  `task_create(..., parent_id="<epic_id>")` — the parent_id hierarchy is what
  the conductor renders. Title = the feature; mechanics go in the description.
  For SAFE parallel fan-out give each child two things: (1) DISJOINT
  `allowed_files` — the hard collision boundary so concurrent dev agents never
  touch the same file (if two slices must share a file they are NOT independent:
  merge them, or sequence them with `dependencies`); (2) its own `proof_type` +
  `oracle` matched to how THAT slice is proven (test|metric|artifact|demo) so
  each child clears its OWN gate shape without override. Don't over-decompose —
  a single demonstrable feature stays ONE task.

## 2. Drive each task through the conductor SDLC state machine
review -> story_gate -> plan_gate -> red (write FAILING tests) -> implement
-> green_gate, via `conductor_advance` / `conductor_gate`.
- story_gate / plan_gate are RUBRIC-VERIFIED: the plan_doc needs a Summary,
  Requirements, Acceptance Criteria (AC-ids WITH oracles), plus a mermaid
  `plan_diagram` — a thin plan scores red. `principles_seed` (or
  `prism_onboard`) must have run so plan_gate's principle check is satisfiable.
  DON'T discover the format by failing the gate: when you `conductor_advance`
  INTO draft_story / verify_plan, the result carries `rubric` — the exact
  required sections, the `AC-<n>` id pattern, and the `oracle:` marker the
  scorer wants. Read it and shape plan_doc to match BEFORE you approve.
- red_gate / green_gate are PROOF-CARRYING, and the proof SHAPE is driven by the
  task's `proof_type` — declare it up front (on task_create / task_update) so
  the gate checks the RIGHT oracle instead of always demanding a failing test:
  - `test` (DEFAULT / TDD) — a failing-then-passing test trace. Omit proof_type
    and you get this; it's the correct choice for most code.
  - `metric` (incl. build-counts) — a numeric/count-delta receipt in
    completion_proof (e.g. "MUD0002 warnings 12 -> 0"); NO red test need exist
    in the tree, so an already-green fix is provable without faking a failure.
  - `artifact` — a produced file/path. `demo` — a UI screenshot / :port capture.
  A `ui`-tagged task still needs SOME artifact, but with a non-`demo`
  proof_type it's judged on THAT oracle's shape — the tag no longer silently
  forces a screenshot. green_gate stays terminal: clear it with a DISTINCT
  actor (no self-override); the proof_type just picks WHICH artifact counts.

## 3. FAN OUT with subagents — and verify with a DISTINCT actor
- A dev/qa subagent builds the slice. Parallelize INDEPENDENT subtasks across
  subagents (fan-out) to move the epic faster — spawn them concurrently.
- SAFE-FAN-OUT INVARIANT: parallelize ONLY children with DISJOINT `allowed_files`
  (the collision boundary), and gate EACH on its declared `proof_type` — a metric
  slice on a count-delta, a ui slice on an artifact, a test slice on a red/green
  trace — so heterogeneous slices clear their own gates instead of override-all.
- Track the whole cohort LEANLY: `task_list(parent_id="<epic_id>", fields=[...])`
  and `conductor_advance/​conductor_gate(..., fields=["from_step","to_step",
  "gate_state"])` return just what you need and DROP the echoed task object —
  that lean read is what lets you fan out WIDE without the driver's own context
  blowing (the verbosity tax is the real ceiling on fan-out width).
- An INDEPENDENT subagent (a DISTINCT actor — NO self-override) clears
  red_gate/green_gate with REAL artifacts: it produces the proof_type's proof
  (test trace / count-delta / artifact path / screenshot), then a distinct actor
  verifies. A gate override by the SAME actor that produced the work is rejected.

## 4. Roll child proofs up to the EPIC green_gate (child roll-up)
Don't re-prove the epic itself. When every non-cancelled child is done with a
STRONG completion_proof, the children ARE the parent's proof — the epic's
green_gate does a child roll-up and passes WITHOUT override (a weak/incomplete
child fails with a concrete reason). Approve the parent once subtasks are green.

## 5. Write the WHY back to memory at green_gate
On green, `memory_store(type="decision", ...)` the DECISION + rationale +
rejected alternatives + the file:line it lives at. It resurfaces in the
Understand wiki so the next agent inherits the reasoning, not just the diff.

## 6. Browse what you already know — the OKF / Understand wiki
Before re-deriving anything, read the unified Understand wiki: `okf_index`
(manifest) -> `okf_get(path)` (one concept + cross-links/backlinks) ->
`okf_graph` (the concept graph). `brain_understand` answers a code-level
question across the graph. Memory you WRITE (`memory_store`) surfaces here.

## 7. Keep conductor responses LEAN — don't drown in echoed task objects
Driving a task is many small steps, and by default every `conductor_advance` /
`conductor_gate` echoes the WHOLE task (plan_doc, diagram, all fields) you just
wrote, while an unscoped `task_list` can blow the token budget on a big board.
Two projections, used by default, cut that:
- `fields=["from_step","to_step","gate_state"]` on `conductor_advance` /
  `conductor_gate` returns ONLY those keys and OMITS the task object — request
  the full task only when you actually need to re-read it. The same `fields`
  projects `task_list` rows.
- `parent_id="<epic_id>"` on `task_list` returns ONLY that epic's children
  (or `parent_id=""` for root tasks) — scope to the epic you're driving instead
  of dumping every task.

## Prefer the skills
- `implement` (workflow) DRIVES one task through this whole conductor SDLC.
- `prototype` (workflow) PLANS one task (research -> PRD-style plan).
Reach for them instead of hand-stepping every gate.
""",
}


def _prism_guide(section: str | None) -> str:
    order = ["overview", "tools", "workflow", "orchestration", "memory",
             "graph", "examples"]
    if section and section in _GUIDE_SECTIONS:
        return _GUIDE_SECTIONS[section]
    return "\n\n".join(_GUIDE_SECTIONS[s] for s in order)


# ---------------------------------------------------------------------------
# Client-side install manifest — served by prism_install / project_onboard so
# the agent can Write the SessionStart hook directly into the user's project.
# ---------------------------------------------------------------------------

from prism_service.__version__ import PRISM_VERSION, PRISM_VERSION_NOTES


_HOOK_SCRIPT = r'''#!/usr/bin/env python3
"""PRISM SessionStart hook — keeps Brain/Graph in sync with disk.

Installed by PRISM version: __PRISM_VERSION__


Walks the project source tree (respects .gitignore when git is available),
hashes each file, asks PRISM via prism_status which files have drifted,
and pushes the current content of drifted files via prism_refresh.

Installed by PRISM's prism_install / project_onboard manifest. The hook
reads its target MCP URL + project slug from .mcp.json at the project
root, so no hardcoded values live here — one hook works across projects.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".cs", ".go", ".rs",
               ".java", ".rb", ".php", ".cpp", ".c", ".h", ".hpp",
               ".md", ".yml", ".yaml", ".toml"}
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
              ".next", ".nuxt", "target", ".claude"}
MAX_FILE_BYTES = 300_000


def _project_root() -> Path:
    # Walk up from cwd looking for .mcp.json
    cur = Path.cwd()
    for d in [cur, *cur.parents]:
        if (d / ".mcp.json").exists():
            return d
    return cur


def _mcp_url_and_project(root: Path) -> tuple[str, str] | None:
    cfg = root / ".mcp.json"
    if not cfg.exists():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return None
    servers = (data.get("mcpServers") or {}).values()
    for s in servers:
        url = s.get("url", "")
        if "/mcp" in url:
            # Split out ?project= query
            if "project=" in url:
                base, q = url.split("?", 1)
                project = [p.split("=", 1)[1] for p in q.split("&")
                           if p.startswith("project=")][0]
                return base.rstrip("/"), project
    return None


def _mcp_call(base: str, project: str, tool: str, args: dict) -> dict:
    url = f"{base}/?project={project}&tool_profile=automation"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode()
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            for line in raw.splitlines():
                if line.startswith("data: "):
                    return json.loads(line[6:])
        return json.loads(raw)


def _parse_result(resp: dict):
    content = resp.get("result", {}).get("content", [])
    if not content:
        return None
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except Exception:
        return text


def _git_tracked(root: Path) -> set[str] | None:
    """Return set of git-tracked relative paths, or None if no git repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
        return {line.strip() for line in out.splitlines() if line.strip()}
    except Exception:
        return None


def _should_skip(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    if any(p in SKIP_PARTS for p in rel_parts):
        return True
    if path.suffix not in SOURCE_EXTS:
        return True
    try:
        sz = path.stat().st_size
    except OSError:
        return True
    if sz == 0 or sz > MAX_FILE_BYTES:
        return True
    return False


def _hash_file(p: Path) -> str | None:
    """Hash the TEXT form (newline-normalized utf-8) so hashes match
    what the server stores — avoids spurious CRLF-vs-LF drift on Windows."""
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collect(root: Path) -> dict[str, tuple[str, Path]]:
    """Return {rel_path: (sha256, abs_path)} for source files under root."""
    out: dict[str, tuple[str, Path]] = {}
    tracked = _git_tracked(root)
    if tracked:
        for rel in tracked:
            p = root / rel
            if not p.is_file() or _should_skip(p, root):
                continue
            sha = _hash_file(p)
            if sha:
                out[rel.replace("\\", "/")] = (sha, p)
    else:
        for p in root.rglob("*"):
            if not p.is_file() or _should_skip(p, root):
                continue
            sha = _hash_file(p)
            if sha:
                out[p.relative_to(root).as_posix()] = (sha, p)
    return out


def main() -> int:
    root = _project_root()
    cfg = _mcp_url_and_project(root)
    if cfg is None:
        # No .mcp.json — user hasn't opted in. Silent skip.
        return 0
    base, project = cfg

    files = _collect(root)
    if not files:
        return 0

    hashes = {path: sha for path, (sha, _) in files.items()}
    try:
        resp = _mcp_call(base, project, "prism_status",
                         {"file_hashes": hashes})
    except Exception as e:
        print(f"[prism-sync] could not reach {base} ({e!r}); skipping",
              file=sys.stderr)
        return 0

    status = _parse_result(resp) or {}
    version = status.get("prism_version") or "?"
    print(
        f"[prism-sync] PRISM v{version} loaded for project '{project}'",
        file=sys.stderr,
    )
    drifted = status.get("drifted", []) or []
    if not drifted:
        # Always surface the version to Claude, even when there's no drift —
        # so the agent's first turn knows which PRISM build is live.
        print(json.dumps({
            "hookSpecificOutput": {
                "additionalContext": (
                    f"PRISM v{version} active for project '{project}'."
                ),
            },
        }))
        return 0

    # Re-ingest drifted files
    to_refresh: dict[str, str] = {}
    for entry in drifted:
        path = entry.get("path")
        if not path:
            continue
        fe = files.get(path)
        if not fe:
            continue
        try:
            to_refresh[path] = fe[1].read_text(encoding="utf-8")
        except Exception:
            pass
    if not to_refresh:
        return 0

    # Chunked refresh: push files in batches of CHUNK_SIZE with
    # skip_graph=true, then fire one graph_rebuild at the end. Avoids
    # the per-call graphify cost that dominates latency on larger syncs.
    CHUNK_SIZE = 25
    items = list(to_refresh.items())
    refreshed = 0
    for i in range(0, len(items), CHUNK_SIZE):
        batch = dict(items[i:i + CHUNK_SIZE])
        try:
            _mcp_call(
                base, project, "prism_refresh",
                {"files": batch, "skip_graph": True},
            )
            refreshed += len(batch)
        except Exception as e:
            print(
                f"[prism-sync] prism_refresh chunk {i // CHUNK_SIZE} "
                f"failed: {e!r}", file=sys.stderr,
            )
    if refreshed:
        try:
            _mcp_call(base, project, "graph_rebuild", {})
        except Exception as e:
            print(
                f"[prism-sync] graph_rebuild after sync failed: {e!r}",
                file=sys.stderr,
            )
        print(
            f"[prism-sync] refreshed {refreshed} drifted file(s) in "
            f"{(len(items) + CHUNK_SIZE - 1) // CHUNK_SIZE} chunk(s) + "
            "1 graph_rebuild",
            file=sys.stderr,
        )

    # LL-10: SessionStart reflection check. If a consolidation
    # candidate is ready, emit hookSpecificOutput.additionalContext so
    # Claude sees the brief on its first turn and can delegate to the
    # prism-reflect sub-agent. Silent no-op when nothing is pending.
    # SessionStart hooks receive a small JSON payload on stdin; extract
    # session_id so janitor_check can rate-limit and so the emitted
    # additionalContext can be linked to this session.
    session_id = ""
    try:
        import json as _json
        import sys as _sys
        session_id = (
            _json.loads(_sys.stdin.read() or "{}").get("session_id", "")
        )
    except Exception:
        pass
    if session_id:
        try:
            chk_resp = _mcp_call(
                base, project, "janitor_check", {"session_id": session_id},
            )
            payload = _parse_result(chk_resp) or {}
            if payload.get("ready") and payload.get("brief"):
                brief = payload["brief"]
                additional = (
                    f"PRISM reflection pending: candidate "
                    f"{brief.get('candidate_id', '?')}. Spawn the "
                    f"`prism-reflect` subagent using the brief below — "
                    f"call `janitor_check` if you need the live version, "
                    f"submit via `janitor_submit`. Brief: "
                    f"{json.dumps(brief)[:6000]}"
                )
                print(json.dumps({
                    "hookSpecificOutput": {
                        "additionalContext": additional,
                    },
                }))
        except Exception as e:
            print(
                f"[prism-sync] janitor_check failed: {e!r}",
                file=sys.stderr,
            )
        try:
            ga_resp = _mcp_call(
                base, project, "graph_annotate_check",
                {"session_id": session_id},
            )
            ga_payload = _parse_result(ga_resp) or {}
            if ga_payload.get("ready") and ga_payload.get("brief"):
                ga_brief = ga_payload["brief"]
                ga_additional = (
                    f"[prism-graph-annotate] brief "
                    f"{ga_brief.get('brief_id', '?')} pending for scope "
                    f"{ga_brief.get('scope_id', '?')} "
                    f"({ga_brief.get('scope_kind', '?')}). Infer a short "
                    f"{{name, purpose}} for this cluster from the prompt "
                    f"below and submit via `graph_annotate_submit`; call "
                    f"`graph_annotate_abandon` if you cannot. Brief: "
                    f"{json.dumps(ga_brief)[:6000]}"
                )
                print(json.dumps({
                    "hookSpecificOutput": {
                        "additionalContext": ga_additional,
                    },
                }))
        except Exception as e:
            print(
                f"[prism-sync] graph_annotate_check failed: {e!r}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _load_asset(filename: str) -> str:
    """Read a shipped hook script from ``services/prism-service/prism_service/assets/``.

    The MCP server is the single source of truth for everything
    ``prism_install`` distributes. The plugin hook directory is a no-op;
    do not add sibling hook implementations there."""
    from pathlib import Path as _P
    try:
        return (_P(__file__).parent.parent / "assets" / filename).read_text(
            encoding="utf-8"
        )
    except Exception:
        return ""


_FEEDBACK_HOOK_SCRIPT = _load_asset("feedback_signal_hook.py")
_STOP_HOOK_SCRIPT = _load_asset("stop_record_hook.py")
_SUBAGENT_HOOK_SCRIPT = _load_asset("subagent_record_hook.py")
_SKILL_HOOK_SCRIPT = _load_asset("skill_usage_hook.py")
_HOOK_LOGGER_SCRIPT = _load_asset("hook_logger.py")
# Autonomous learning loop: edit-learn (PostToolUse) + idle-rebuild (Stop)
# pair up so source-file edits flow into Brain mid-session, then one
# graph_rebuild flushes them at session end.
_EDIT_LEARN_HOOK_SCRIPT = _load_asset("edit_learn_hook.py")
_IDLE_REBUILD_HOOK_SCRIPT = _load_asset("idle_rebuild_hook.py")
_VERIFIER_HOOK_SCRIPT = _load_asset("verifier_hook.py")
# LL-10 — subagent definition + slash command shipped alongside the
# hook scripts so Claude has something to match on when it sees the
# SessionStart additionalContext nudge or the MCP-response header.
_REFLECT_AGENT_MD = _load_asset("prism_reflect_agent.md")
_REFLECT_COMMAND_MD = _load_asset("prism_reflect_command.md")


def _hook_python_cmd(host_platform: str | None) -> str:
    """Pick the Python invocation that hook commands should use on the host.

    PEP 394 makes `python3` canonical on POSIX (modern Linux distros and
    Debian dropped bare `python`; only `/usr/bin/python3` ships).
    PEP 397 makes `py.exe` canonical on Windows: it is installed by every
    python.org installer, lives in PATH by default, and routes shebangs.
    `python3.exe` does NOT ship by default on Windows, so the previous
    `python3 ...` command broke every Windows host. `py -3 ...` works on
    every supported Windows install and reads the shebang in our hooks
    (`#!/usr/bin/env python3`) to pick the right interpreter.

    Caller passes the host's platform (Claude Code knows its own OS).
    Anything starting with `win` / `nt` is treated as Windows; everything
    else (including None) falls back to POSIX `python3`.
    """
    token = (host_platform or "").strip().lower()
    if token.startswith(("win", "nt")):
        return "py -3"
    return "python3"


def _install_manifest(project_id: str, host_platform: str | None = None) -> dict:
    """Return the install manifest the agent should apply on first onboard.
    The PRISM service is the single source of truth — if the hook logic
    changes in a future release, a re-onboard serves the new version.

    `host_platform` is the OS where Claude Code (and therefore the hooks)
    will run. Pass `sys.platform` from the caller, or `"windows"` /
    `"linux"` / `"darwin"`. Defaults to POSIX (`python3`)."""
    hook_script = _HOOK_SCRIPT.replace("__PRISM_VERSION__", PRISM_VERSION)
    py = _hook_python_cmd(host_platform)
    # Claude Code reads hooks from .claude/settings.json under a top-level
    # "hooks" key. A bare .claude/hooks.json is ignored (only plugin-shipped
    # hooks/hooks.json is loaded, via a different code path). Wrap the event
    # map under "hooks" and target settings.json so the hooks actually fire.
    # v5.3.16 — slimmed from 6 hooks to 3. The disk-reader added in
    # v5.3.15 (services/claude_transcripts.py) reads each session's
    # ~/.claude/projects/<slug>/<uuid>.jsonl directly and populates
    # session_outcomes + skill_usage post-hoc. That makes the Stop /
    # SubagentStop / PostToolUse-Skill / Stop-idle-rebuild hooks
    # redundant — they were just shovelling data into tables the
    # disk-reader now fills natively.
    #
    # What remains genuinely needs to fire IN-LINE:
    #   * SessionStart sync — must run BEFORE the session starts so
    #     brain_search sees latest drift on the first query.
    #   * PostToolUse edit-learn — must ingest edits MID-session so
    #     subsequent brain_searches in the same session reflect them.
    #   * PostToolUse feedback-signal — populates the Learning page's
    #     retrieval-feedback signal (could move to disk-reader later
    #     but the correlation logic is non-trivial; defer).
    #   * Stop verifier — runs the project's own ruff/mypy/pytest on
    #     git-diff'd files. Not a metrics shovel; doesn't duplicate
    #     anything the disk-reader does.
    hooks_map = {
        "SessionStart": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{py} ${{CLAUDE_PROJECT_DIR}}/.claude/hooks/prism-sync.py",
                        "timeout": 30000,
                    },
                ],
            },
        ],
        "PostToolUse": [
            {
                "matcher": "mcp__prism__brain_search|Read|Edit|Write",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{py} ${{CLAUDE_PROJECT_DIR}}/.claude/hooks/prism-feedback-signal.py",
                        "description": (
                            "Implicit retrieval feedback: correlate "
                            "brain_search results with Read/Edit and emit "
                            "brain_search_feedback automatically. Feeds "
                            "the Learning page."
                        ),
                    },
                ],
            },
            {
                "matcher": "Edit|Write|NotebookEdit",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{py} ${{CLAUDE_PROJECT_DIR}}/.claude/hooks/prism-edit-learn.py",
                        "description": (
                            "Auto-ingest edited source files into Brain via "
                            "prism_refresh (skip_graph) so brain_search "
                            "reflects in-session edits without waiting for "
                            "the next SessionStart."
                        ),
                    },
                ],
            },
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{py} ${{CLAUDE_PROJECT_DIR}}/.claude/hooks/prism-verifier.py",
                        "description": (
                            "Outer-harness verifier sensor. Tier 0 runs "
                            "the project's own tooling (ruff, mypy, "
                            "pytest, eslint, tsc, cargo check, go vet) "
                            "on git-diff'd files; Tier 1 walks PRISM "
                            "tables and confirms each claim against "
                            "current state. Advisory: writes verdict to "
                            ".prism/verifier.log, never blocks the agent."
                        ),
                    },
                ],
            },
        ],
        # v5.3.16 — Stop record / SubagentStop / Skill usage / idle "
        # rebuild hooks dropped: claude_transcripts disk-reader (60s "
        # importer in main.py) covers session_outcomes + skill_usage "
        # natively. graph_rebuild after a session-with-edits can be "
        # triggered from the same importer in a follow-up.
    }
    settings_json = {"hooks": hooks_map}
    return {
        "prism_version": PRISM_VERSION,
        "version_notes": PRISM_VERSION_NOTES,
        "project_id": project_id,
        "instructions_for_agent": [
            "prism_install is idempotent. Safe to re-run on any project — "
            "the file actions ('upsert' / 'create_or_merge') below describe "
            "how each file heals drift. Treat this as a migration step, "
            "not a first-install-only step.",
            "For every install_file with action='upsert': write the file "
            "unconditionally with the manifest content (overwriting any "
            "existing copy). These are server-authoritative artefacts — a "
            "local edit means the install is stale.",
            "For .claude/settings.json (action='create_or_merge'): if the "
            "file exists, read it, deep-merge the 'hooks' object (append new "
            "entries into each event array without duplicating commands), "
            "and write it back. Preserve every other top-level key "
            "('permissions', 'statusLine', 'enabledPlugins', etc). If the "
            "file does not exist, write the manifest content verbatim.",
            "Migration: Claude Code only reads hooks from settings.json. If "
            ".claude/hooks.json exists (from a pre-fix install), its entries "
            "are dead. Read it, deep-merge its top-level arrays into "
            "settings.json under 'hooks', then delete .claude/hooks.json.",
            "After writing, tell the user: 'Restart Claude so the new "
            "PRISM hooks activate.'",
            "STAY CURRENT WITH NEW MCP ENDPOINTS: this manifest is served by "
            "PRISM v" + PRISM_VERSION + ". Your MCP client caches the tool list "
            "at connect time, so after PRISM upgrades (the daemon auto-updates, "
            "or `prism update`) NEW endpoints will not appear until you "
            "reconnect — in Claude Code run `/mcp` and reconnect the `prism` "
            "server (or restart Claude Code), then call prism_guide again. "
            "Detect drift by comparing prism_status.prism_version to the version "
            "you onboarded on. Re-running prism_install (this tool) is the "
            "client-side half of that update — do it whenever prism_version "
            "changes so the hooks AND tool list refresh together.",
            "The default MCP profile is a small curated surface. To use "
            "maintenance/admin or legacy endpoints (brain_index_doc, "
            "graph_rebuild, project_create, verifier_run, the legacy "
            "understand_* family, …) reconnect with the url "
            "`/mcp/?project=<slug>&tool_profile=all`. prism_onboard returns the "
            "ready-made mcp_url_all for this.",
            "Hook surface (v5.3.16, slim): SessionStart syncs Brain/Graph "
            "drift before the session starts. PostToolUse covers two "
            "in-session needs — implicit retrieval feedback "
            "(brain_search → Read/Edit, feeds the Learning page) and "
            "edit-learn (auto-ingests edited files into Brain so "
            "subsequent searches see them). Stop fires the verifier "
            "sensor (advisory). Session outcomes + skill usage are NOT "
            "captured by hooks anymore — the service's disk-reader "
            "(services/claude_transcripts.py, started by main.py "
            "lifespan) walks ~/.claude/projects/<slug>/*.jsonl every "
            "60s and populates session_outcomes natively. That removes "
            "the prism-stop, prism-subagent, prism-skill-usage, and "
            "prism-idle-rebuild hooks the older manifest used to ship.",
            "Cleanup: if you find these stale hook scripts present from "
            "a pre-v5.3.16 install, delete them — they reference the "
            "old commands and won't be wired into settings.json "
            "anymore: .claude/hooks/prism-stop.py, .claude/hooks/"
            "prism-subagent.py, .claude/hooks/prism-skill-usage.py, "
            ".claude/hooks/prism-idle-rebuild.py. Leaving them on disk "
            "is harmless (settings.json doesn't reference them) but "
            "tidier to remove.",
        ],
        "install_files": [
            {
                "path": ".claude/settings.json",
                "action": "create_or_merge",
                "merge_keys": ["hooks"],
                "content": json.dumps(settings_json, indent=2),
            },
            {
                "path": ".claude/hooks/prism-sync.py",
                "action": "upsert",
                "content": hook_script,
                "mode": "0755",
            },
            {
                "path": ".claude/hooks/prism-feedback-signal.py",
                "action": "upsert",
                "content": _FEEDBACK_HOOK_SCRIPT,
                "mode": "0755",
            },
            # Autonomous-learning loop. PostToolUse on Edit/Write/NotebookEdit
            # auto-ingests the changed file into Brain (skip_graph=true) and
            # drops a .prism/graph-dirty sentinel. (v5.3.16: the matching
            # Stop idle-rebuild is gone — the disk-reader's follow-up
            # commit will trigger graph_rebuild on session-end-with-edits.)
            {
                "path": ".claude/hooks/prism-edit-learn.py",
                "action": "upsert",
                "content": _EDIT_LEARN_HOOK_SCRIPT,
                "mode": "0755",
            },
            {
                "path": ".claude/hooks/prism-verifier.py",
                "action": "upsert",
                "content": _VERIFIER_HOOK_SCRIPT,
                "mode": "0755",
            },
            # Shared logger: hooks call log_hook_failure() instead of the
            # silent `except: pass` that hid a month of dogfood breakage.
            # Imported by every record hook; writes to
            # .prism/logs/hooks.log.
            {
                "path": ".claude/hooks/hook_logger.py",
                "action": "upsert",
                "content": _HOOK_LOGGER_SCRIPT,
                "mode": "0755",
            },
            # LL-10 — ship the reflection sub-agent + slash command so
            # Claude has something to match on when it sees the
            # SessionStart additionalContext nudge or the MCP-response
            # header from LL-09.
            {
                "path": ".claude/agents/prism-reflect.md",
                "action": "upsert",
                "content": _REFLECT_AGENT_MD,
            },
            {
                "path": ".claude/commands/prism-reflect.md",
                "action": "upsert",
                "content": _REFLECT_COMMAND_MD,
            },
        ],
        "verification_steps": [
            "After Claude restart, re-invoke any tool and confirm no errors.",
            "Call prism_status with no args — expect stale: false.",
            "Edit any indexed source file, restart Claude, check the hook "
            "logs for '[prism-sync] refreshed 1 drifted file(s)'.",
            "Finish a Claude response, reload /sessions — expect a new row "
            "for the session_id just recorded.",
            "After any merged task, run `/prism-reflect` — it should drain "
            "one pending candidate via the prism-reflect subagent.",
        ],
    }


# ---------------------------------------------------------------------------
# Indexer in-flight tracking — exposed via prism_status (#15 observability).
# Bumped when a request is actively inside prism_refresh's synchronous
# index/graph work, so a concurrent prism_status call can report
# indexing_in_flight=True without scanning state.
# ---------------------------------------------------------------------------
import threading as _th

_INDEXING_LOCK = _th.Lock()
_INDEXING_IN_FLIGHT: dict[str, int] = {}  # project_id -> in-flight request count


def _indexing_begin(project_id: str) -> None:
    with _INDEXING_LOCK:
        _INDEXING_IN_FLIGHT[project_id] = (
            _INDEXING_IN_FLIGHT.get(project_id, 0) + 1
        )


def _indexing_end(project_id: str) -> None:
    with _INDEXING_LOCK:
        n = _INDEXING_IN_FLIGHT.get(project_id, 0) - 1
        if n <= 0:
            _INDEXING_IN_FLIGHT.pop(project_id, None)
        else:
            _INDEXING_IN_FLIGHT[project_id] = n


def indexing_in_flight(project_id: str) -> int:
    with _INDEXING_LOCK:
        return int(_INDEXING_IN_FLIGHT.get(project_id, 0))


# Cancellation flag per project. Set by prism_cancel_pending, consumed
# at the next unit-of-work boundary inside prism_refresh. Pop-on-read
# so a single cancel request cancels a single in-flight refresh.
_CANCEL_FLAGS: dict[str, bool] = {}


def request_cancel(project_id: str) -> None:
    with _INDEXING_LOCK:
        _CANCEL_FLAGS[project_id] = True


def check_and_clear_cancel(project_id: str) -> bool:
    """Return True exactly once if a cancel was requested; clear it."""
    with _INDEXING_LOCK:
        return bool(_CANCEL_FLAGS.pop(project_id, False))


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

# Tools whose responses must never get a reflection nudge prepended —
# either because they're part of the reflection pipeline itself (would
# create a feedback loop) or because the caller needs the response
# structure unchanged (install manifest, guide prose).
_NO_AUGMENT_TOOLS: frozenset[str] = frozenset({
    "janitor_enqueue", "janitor_mark_stale", "janitor_check",
    "janitor_submit", "janitor_abandon", "janitor_status",
    "graph_annotate_enqueue", "graph_annotate_check",
    "graph_annotate_submit", "graph_annotate_abandon",
    "graph_annotate_status",
    "memory_invalidate", "prism_install", "prism_guide",
    # prism_onboard returns a structured bootstrap payload (snippet, ports,
    # staying_current steps) parsed by the onboarding agent — keep it clean.
    "prism_onboard",
})


async def handle_tool(name: str, arguments: dict, *, project_id: str = "default") -> list[TextContent]:
    """Outer MCP entry point. Dispatches to :func:`_dispatch_tool`, then
    lets :func:`_maybe_augment_with_nudge` prepend a pending-reflection
    header when appropriate (LL-09).

    All real work runs in the default thread pool — every dispatch arm
    does sync sqlite I/O, so executing them on uvicorn's event loop
    would let a slow/contended SQLite call freeze the accept loop and
    silently drop new MCP requests at the kernel layer (issue #38).
    """
    import asyncio as _aio
    result = await _aio.to_thread(
        _dispatch_tool, name, arguments, project_id=project_id,
    )
    if name in _NO_AUGMENT_TOOLS:
        return result
    try:
        return await _aio.to_thread(
            _maybe_augment_with_nudge, result, project_id=project_id,
        )
    except Exception:
        # Augmentation is strictly advisory — any failure here must not
        # affect the tool result the caller actually needs.
        return result


def _maybe_augment_with_nudge(
    result: list[TextContent], *, project_id: str,
) -> list[TextContent]:
    """Prefix the first TextContent with a PRISM_REFLECTION_PENDING
    header when a pending candidate exists for this project AND we
    haven't nudged it in the last 5 minutes. Updates last_nudged_at on
    the candidate. Disabled globally by PRISM_MCP_AUGMENT_NUDGES=false.
    """
    import os as _os
    import sqlite3 as _sq3
    from datetime import datetime, timedelta, timezone

    if _os.environ.get("PRISM_MCP_AUGMENT_NUDGES", "").lower() == "false":
        return result
    if not result:
        return result

    from prism_service.project_context import get_project
    try:
        ctx = get_project(project_id)
    except Exception:
        return result

    scores_path = str(ctx._data_dir / "scores.db")
    if not Path(scores_path).exists():
        return result

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=5)).isoformat()

    conn = _sq3.connect(scores_path, timeout=5.0)
    conn.row_factory = _sq3.Row
    try:
        # Oldest pending candidate not nudged in the last 5 min.
        row = conn.execute(
            "SELECT id, task_id FROM consolidation_candidates "
            "WHERE status='pending' "
            "  AND (last_nudged_at IS NULL OR last_nudged_at <= ?) "
            "ORDER BY queued_at ASC LIMIT 1",
            (cutoff,),
        ).fetchone()
        if row is None:
            return result
        cid = row["id"]
        tid = row["task_id"] or ""
        conn.execute(
            "UPDATE consolidation_candidates SET last_nudged_at=? WHERE id=?",
            (now.isoformat(), cid),
        )
        conn.commit()
    finally:
        conn.close()

    header = (
        f"\u26a0\ufe0f PRISM_REFLECTION_PENDING candidate={cid} task={tid}\n"
        "Before continuing, spawn the `prism-reflect` subagent. Call "
        "`janitor_check` to fetch the brief, submit via `janitor_submit`.\n"
        "---\n"
    )
    first = result[0]
    augmented_text = header + (first.text or "")
    return [TextContent(type="text", text=augmented_text)] + list(result[1:])


def _dispatch_tool(name: str, arguments: dict, *, project_id: str = "default") -> list[TextContent]:
    """Dispatch an MCP tool call to the appropriate service method.

    Sync by design — invoked via ``asyncio.to_thread`` from
    :func:`handle_tool` so the uvicorn event loop is never blocked on
    SQLite I/O. The *project_id* scopes all data access to the correct
    project.
    """
    from prism_service.project_context import get_project, get_all_projects, create_project

    try:
        # ------------------------------------------------------------------
        # Project management tools (not scoped)
        # ------------------------------------------------------------------
        if name == "project_list":
            projects = get_all_projects()
            return [TextContent(type="text", text=_json({
                "projects": projects,
                "current": project_id,
            }))]

        if name == "project_create":
            pid = arguments["project_id"]
            create_project(pid)
            return [TextContent(type="text", text=_json({
                "created": pid,
                "message": f"Project '{pid}' created. Connect with ?project={pid}",
            }))]

        if name == "project_onboard":
            ctx = get_project(project_id)
            project_name = arguments.get("project_name") or project_id
            sub_projects = arguments.get("sub_projects") or []
            conventions = arguments.get("conventions") or []

            # 1. Store project identity
            ctx.memory_svc.store(
                domain="project",
                name="project-identity",
                description=f"Project: {project_name} (id: {project_id})",
                type="convention",
                classification="foundational",
            )

            # 2. Store sub-project map
            if sub_projects:
                sub_desc = "\n".join(
                    f"- {sp.get('name', '?')}: {sp.get('tech', '?')} at {sp.get('path', '?')}"
                    for sp in sub_projects
                )
                ctx.memory_svc.store(
                    domain="project",
                    name="sub-project-map",
                    description=f"Sub-projects:\n{sub_desc}",
                    type="convention",
                    classification="foundational",
                )

            # 3. Seed conventions with enriched descriptions
            # Convention descriptions need anti-pattern keywords so the
            # pre-write-convention-guard hook can detect violations.
            seeded = 0
            for conv in conventions:
                try:
                    name = conv[:50].lower().replace(" ", "-").replace("'", "")
                    # Enrich: if the convention is a short one-liner, expand it
                    # with explicit "Never"/"Do not" phrasing for guard matching
                    description = conv
                    if len(conv) < 120:
                        description = _enrich_convention(conv)
                    ctx.memory_svc.store(
                        domain="conventions",
                        name=name,
                        description=description,
                        type="convention",
                        classification="foundational",
                    )
                    seeded += 1
                except Exception:
                    pass

            # 4. Build sub-project path hints for the instructions
            sp_hints = ""
            if sub_projects:
                sp_lines = []
                for sp in sub_projects:
                    sp_lines.append(f"  - {sp.get('name','?')} ({sp.get('tech','?')}): {sp.get('path','?')}")
                sp_hints = "\nSub-projects:\n" + "\n".join(sp_lines) + "\n"

            # Return direct imperative instructions as plain text.
            # This is NOT a report — Claude must execute these steps.
            host_platform = arguments.get("host_platform")
            manifest = _install_manifest(project_id, host_platform)
            files_list = "\n".join(
                f"  - {f['path']} (action: {f['action']})"
                for f in manifest["install_files"]
            )
            instructions = f"""PRISM onboarding initialized for "{project_name}" (project: {project_id}).
{seeded} conventions seeded into memory.{sp_hints}
IMPORTANT: Onboarding is NOT complete. You MUST now execute all of the following steps.
Do NOT summarize this response to the user — work through each step and call the MCP tools.

== STEP 0: Install the client-side drift-sync hook ==
Call prism_install to fetch the install manifest. It returns:
{files_list}
Use the Write tool to create each file. For .claude/settings.json, MERGE the
"hooks" object if the file already exists (append to each event's array;
don't overwrite other top-level keys like permissions/statusLine).
This one-time setup makes PRISM auto-sync on every future Claude session —
no manual re-indexing needed.

== STEP 1: Index architecture docs ==
Find and read: README.md, CLAUDE.md, architecture.md, CONTRIBUTING.md, any docs/ folder.
For EACH doc found, call brain_index_doc with the full file content:
  brain_index_doc(path="README.md", content="<full file content>", domain="docs")

== STEP 2: Index dependency/config files ==
Find and read: package.json, *.csproj, tsconfig.json, docker-compose.yml, .editorconfig, Cargo.toml, go.mod, pyproject.toml — whatever exists.
For EACH one, call brain_index_doc:
  brain_index_doc(path="package.json", content="<full content>", domain="config")

== STEP 3: Index entry points and routing ==
Find the main entry points: Program.cs, main.ts, app.py, index.ts, etc.
Find routing/endpoint registration files.
For EACH one, call brain_index_doc with entities:
  brain_index_doc(path="Program.cs", content="<content>", domain="code", entities=[{{"name": "Main", "kind": "entrypoint"}}])

== STEP 4: Index 15-30 key source files ==
For each sub-project, pick the most important files:
  - Core domain models / types / interfaces
  - Service layer / business logic
  - API endpoint definitions
  - Database models / migrations
  - Shared utilities / helpers
  - Test setup / fixtures
  - Middleware / interceptors
Read each file and call brain_index_doc. Include entities (functions, classes, interfaces) for graph search.
THIS IS THE MOST IMPORTANT STEP. The Brain is useless without indexed source files.

== STEP 5: Store architectural patterns ==
Based on what you've read, store patterns and decisions via memory_store:
  memory_store(domain="architecture", name="<pattern-name>", description="<detailed description with file paths and code examples>", type="pattern", classification="foundational")
Include FILE PATHS and CODE SNIPPETS in descriptions so memory is actionable, not just rules.

== STEP 6: Store conventions with evidence ==
For conventions not already seeded, discover them from linting configs, code patterns, and test structure.
Store via memory_store with domain="conventions". Include specific file paths as evidence.

== STEP 7: Rebuild the code graph ==
After indexing source files, call graph_rebuild once to run graphify and
populate the entity/relationship tables with Leiden communities.

== STEP 8: Verify ==
Call brain_search with 3 different queries to verify Brain has indexed documents.
Call memory_recall to verify conventions are stored.
Call prism_status to confirm the graph is in sync.
Report the final count to the user: "Indexed X documents, Y entities, Z communities.
PRISM sync hook installed — restart Claude to activate auto-sync."

BEGIN NOW with Step 0. Do not ask the user for permission — execute the steps."""

            return [TextContent(type="text", text=instructions)]

        # ------------------------------------------------------------------
        # Get project-scoped services
        # ------------------------------------------------------------------
        ctx = get_project(project_id)
        brain_svc = ctx.brain_svc
        task_svc = ctx.task_svc
        workflow_svc = ctx.workflow_svc
        memory_svc = ctx.memory_svc
        conductor_svc = ctx.conductor_svc
        governance = ctx.governance

        # ------------------------------------------------------------------
        # Brain tools
        # ------------------------------------------------------------------
        if name == "brain_search":
            results = brain_svc.search(
                query=arguments["query"],
                domain=arguments.get("domain"),
                limit=arguments.get("limit", 5),
                domains=arguments.get("domains"),
            )
            return [TextContent(type="text", text=_json(results))]

        if name == "brain_understand":
            from prism_service.services import understand_view
            payload = understand_view.build_understanding(
                project_id,
                arguments.get("query"),
                limit=max(1, min(int(arguments.get("limit", 20)), 200)),
                depth=max(0, min(int(arguments.get("depth", 1)), 3)),
                seed_files=arguments.get("seed_files") or None,
                label=arguments.get("label"),
                domain=arguments.get("domain") or None,
            )
            return [TextContent(type="text", text=_json(payload))]

        if name == "register_claude_source":
            from prism_service.data_dir import resolve_claude_home
            from prism_service.engines import understand_engine as ue
            from prism_service.services.claude_transcripts import path_to_slug

            target_project = (arguments.get("project") or project_id).strip()
            cwd = (arguments.get("cwd") or "").strip()
            if not cwd:
                return [TextContent(type="text", text=_json({
                    "ok": False, "error": "cwd is required", "project": target_project,
                }))]
            resolved = resolve_claude_home() / "projects" / path_to_slug(cwd)
            jsonl = sorted(resolved.glob("*.jsonl")) if resolved.is_dir() else []
            if not resolved.is_dir() or not jsonl:
                return [TextContent(type="text", text=_json({
                    "ok": False,
                    "error": (
                        "no Claude transcript dir with *.jsonl at the resolved "
                        "path — check cwd / CLAUDE_CONFIG_DIR"
                    ),
                    "resolved_dir": str(resolved),
                    "jsonl_count": len(jsonl),
                    "project": target_project,
                }))]
            # Persist via the SAME writer the Settings editor uses; idempotent.
            # Set BOTH keys off the agent-declared cwd so ONE MCP registration
            # drives EVERY transcript consumer, not just the import poller:
            #   - claude_project_dir = the resolved ~/.claude/projects/<slug>
            #     transcript dir (read by the 60s import poller).
            #   - source_path = the cwd itself, which _project_source_path()
            #     reads and slug-matches to resolve the LIVE conductor token /
            #     burn graph. Before this, an agent could register the right
            #     folder yet the token graph still read a stale/empty
            #     source_path -> empty (#134) or a wrong-session 40/flatline.
            state = ue._read_state(target_project)
            state["claude_project_dir"] = str(resolved)
            state["source_path"] = cwd
            ue._write_state(target_project, state)
            return [TextContent(type="text", text=_json({
                "ok": True,
                "resolved_dir": str(resolved),
                "source_path": cwd,
                "jsonl_count": len(jsonl),
                "project": target_project,
                "source": "explicit",
            }))]

        if name == "brain_index_doc":
            path = arguments["path"]
            content = arguments["content"]
            domain = arguments.get("domain", "code")
            entities = arguments.get("entities") or []
            doc_id = brain_svc.index_doc(
                path=path, content=content, domain=domain, entities=entities,
            )
            return [TextContent(type="text", text=_json({
                "indexed": True,
                "doc_id": doc_id,
                "path": path,
                "domain": domain,
                "content_length": len(content),
                "entities": len(entities),
            }))]

        if name == "brain_search_feedback":
            feedback_id = brain_svc.record_search_feedback(
                search_id=int(arguments["search_id"]),
                doc_id=str(arguments["doc_id"]),
                signal=str(arguments["signal"]),
                note=arguments.get("note"),
            )
            return [TextContent(type="text", text=_json({
                "recorded": feedback_id is not None,
                "feedback_id": feedback_id,
            }))]

        if name == "brain_find_symbol":
            results = brain_svc.find_symbol(
                name=arguments["name"],
                kind=arguments.get("kind"),
                limit=arguments.get("limit", 10),
            )
            return [TextContent(type="text", text=_json(results))]

        if name == "brain_outline":
            results = brain_svc.outline(source_file=arguments["source_file"])
            return [TextContent(type="text", text=_json(results))]

        if name == "brain_find_references":
            results = brain_svc.find_references(
                name=arguments["name"],
                limit=arguments.get("limit", 20),
                include_rationale=arguments.get("include_rationale", False),
            )
            return [TextContent(type="text", text=_json(results))]

        if name == "brain_call_chain":
            results = brain_svc.call_chain(
                entity=arguments["entity"],
                depth=arguments.get("depth", 2),
                limit=arguments.get("limit", 50),
                relation=arguments.get("relation", "calls"),
                direction=arguments.get("direction", "callees"),
            )
            return [TextContent(type="text", text=_json(results))]

        if name == "record_session_outcome":
            ok = brain_svc.record_session_outcome(
                session_id=str(arguments["session_id"]),
                duration_s=int(arguments.get("duration_s", 0)),
                tokens_used=int(arguments.get("tokens_used", 0)),
                files_read=int(arguments.get("files_read", 0)),
                files_modified=int(arguments.get("files_modified", 0)),
                skills_invoked=int(arguments.get("skills_invoked", 0)),
            )
            if ok:
                from prism_service.events import bus as _bus
                _bus.publish({
                    "project": project_id,
                    "type": "session_outcome",
                    "session_id": str(arguments["session_id"]),
                })
            # AUTO WRITER (Stop path): the Stop hook POSTs record_session_outcome
            # at session end. Tie this ending session to every in_progress task
            # so association is captured server-side without an explicit
            # task_link_session call. Best-effort, mirrors the task_update /
            # memory record_outcome side-effect idiom: must NEVER break the
            # primary session_outcomes record.
            # DECISION (a1bed6bb): do NOT backfill historical
            # verifier_runs/consolidation_candidates rows in this slice — out of
            # scope; new associations accrue going forward only.
            try:
                _sid = str(arguments["session_id"])
                for _t in task_svc.list(status="in_progress"):
                    task_svc.link_session(_t.id, _sid)
            except Exception:
                pass  # best-effort — never break the session outcome record
            return [TextContent(type="text", text=_json({"recorded": ok}))]

        if name == "record_skill_usage":
            ok = brain_svc.record_skill_usage(
                session_id=str(arguments["session_id"]),
                skill_name=str(arguments["skill_name"]),
                timestamp=str(arguments.get("timestamp") or ""),
            )
            if ok:
                from prism_service.events import bus as _bus
                _bus.publish({
                    "project": project_id,
                    "type": "skill_usage",
                    "session_id": str(arguments["session_id"]),
                    "skill_name": str(arguments["skill_name"]),
                })
            return [TextContent(type="text", text=_json({"recorded": ok}))]

        if name == "record_outcome":
            ok = brain_svc.record_outcome(
                prompt_id=str(arguments["prompt_id"]),
                persona=str(arguments["persona"]),
                step_id=str(arguments["step_id"]),
                metrics=arguments.get("metrics") or {},
            )
            return [TextContent(type="text", text=_json({"recorded": ok}))]

        if name == "record_subagent_outcome":
            ok = brain_svc.record_subagent_outcome(
                prompt_id=str(arguments["prompt_id"]),
                validator=str(arguments["validator"]),
                recommendation=str(arguments["recommendation"]),
                evidence_count=int(arguments.get("evidence_count", 0)),
                certificate_complete=int(arguments.get("certificate_complete", 0)),
                certificate_blocked=int(arguments.get("certificate_blocked", 0)),
                timed_out=int(arguments.get("timed_out", 0)),
                tokens_used=int(arguments.get("tokens_used", 0)),
                duration_s=float(arguments.get("duration_s", 0.0)),
            )
            return [TextContent(type="text", text=_json({"recorded": ok}))]

        if name == "meta_conductor_brief":
            ctx = get_project(project_id)
            payload = ctx.conductor_svc.meta_brief(
                persona=str(arguments["persona"]),
                step_id=str(arguments["step_id"]),
                limit=int(arguments.get("limit", 5)),
            )
            return [TextContent(type="text", text=_json(payload))]

        if name == "meta_conductor_propose":
            ctx = get_project(project_id)
            payload = ctx.conductor_svc.propose_meta_candidate(
                persona=str(arguments["persona"]),
                step_id=str(arguments["step_id"]),
                content=str(arguments["content"]),
                parent_prompt_id=str(arguments.get("parent_prompt_id") or ""),
                rationale=str(arguments.get("rationale") or ""),
                generator=str(arguments.get("generator") or ""),
            )
            return [TextContent(type="text", text=_json(payload))]

        if name == "meta_conductor_evaluate":
            ctx = get_project(project_id)
            payload = ctx.conductor_svc.evaluate_meta_candidate(
                candidate_id=str(arguments["candidate_id"]),
                metrics=arguments.get("metrics") or {},
            )
            return [TextContent(type="text", text=_json(payload))]

        if name == "meta_conductor_auto":
            ctx = get_project(project_id)
            payload = ctx.conductor_svc.auto_meta_candidate(
                persona=str(arguments["persona"]),
                step_id=str(arguments["step_id"]),
                limit=int(arguments.get("limit", 5)),
                metrics=arguments.get("metrics"),
            )
            return [TextContent(type="text", text=_json(payload))]

        if name == "brain_list":
            docs = brain_svc.list_docs(
                domain=arguments.get("domain"),
                limit=arguments.get("limit", 100),
            )
            return [TextContent(type="text", text=_json(docs))]

        if name == "brain_graph":
            results = brain_svc.graph_query(
                entity=arguments["entity"],
                relation=arguments.get("relation"),
                limit=arguments.get("limit", 10),
                include_rationale=arguments.get("include_rationale", False),
            )
            return [TextContent(type="text", text=_json(results))]

        if name == "graph_rebuild":
            ctx = get_project(project_id)
            summary = ctx.graph_svc.rebuild(
                brain_db_path=str(ctx._data_dir / "brain.db")
            )
            return [TextContent(type="text", text=_json(summary))]

        if name == "prism_status":
            ctx = get_project(project_id)
            status = ctx.graph_svc.sync_status(
                brain_db_path=str(ctx._data_dir / "brain.db"),
                file_hashes=arguments.get("file_hashes"),
            )
            # #15(c) observability: operators can tell when indexer is busy
            # without scanning logs. indexing_in_flight counts concurrent
            # prism_refresh calls currently inside their CPU-bound work.
            n = indexing_in_flight(project_id)
            status["indexing_in_flight"] = n
            status["indexer_busy"] = bool(n)
            return [TextContent(type="text", text=_json(status))]

        if name == "prism_refresh":
            ctx = get_project(project_id)
            files = arguments.get("files") or {}
            default_domain = arguments.get("domain") or "code"
            skip_graph = bool(arguments.get("skip_graph", False))
            _indexing_begin(project_id)
            indexed = 0
            cancelled = False
            try:
                for path, content in files.items():
                    if check_and_clear_cancel(project_id):
                        cancelled = True
                        break
                    if not isinstance(content, str):
                        continue
                    # _dispatch_tool already runs in a worker thread
                    # (handle_tool wraps it in asyncio.to_thread), so
                    # concurrent prism_status / brain_search calls run
                    # on other workers and don't queue behind this
                    # CPU-bound ingest.
                    ctx.brain_svc.index_doc(
                        path=path, content=content, domain=default_domain,
                    )
                    indexed += 1
                if cancelled:
                    summary = {"cancelled": True, "graph_skipped": True}
                elif skip_graph:
                    summary = {"graph_skipped": True}
                else:
                    summary = ctx.graph_svc.rebuild(
                        brain_db_path=str(ctx._data_dir / "brain.db"),
                    )
            finally:
                _indexing_end(project_id)
            summary["refreshed_files"] = indexed
            return [TextContent(type="text", text=_json(summary))]

        if name == "prism_bulk_refresh":
            import os as _os
            ctx = get_project(project_id)
            files = arguments.get("files") or {}
            default_domain = arguments.get("domain") or "code"
            chunk_size = max(1, int(arguments.get("chunk_size", 25)))
            skip_graph = bool(arguments.get("skip_graph", False))
            max_concurrent = int(
                _os.environ.get("PRISM_MAX_CONCURRENT_REFRESH", "2")
            )
            if indexing_in_flight(project_id) >= max_concurrent:
                return [TextContent(type="text", text=_json({
                    "busy": True,
                    "in_flight": indexing_in_flight(project_id),
                    "max_concurrent": max_concurrent,
                    "retry_after_s": 30,
                    "note": "server saturated — back off then retry",
                }))]
            _indexing_begin(project_id)
            indexed = 0
            cancelled = False
            chunks = 0
            try:
                items = list(files.items())
                for i in range(0, len(items), chunk_size):
                    if check_and_clear_cancel(project_id):
                        cancelled = True
                        break
                    batch = items[i:i + chunk_size]
                    for path, content in batch:
                        if not isinstance(content, str):
                            continue
                        ctx.brain_svc.index_doc(
                            path=path, content=content, domain=default_domain,
                        )
                        indexed += 1
                    chunks += 1
                if cancelled or skip_graph:
                    summary = {
                        "cancelled": cancelled,
                        "graph_skipped": True,
                    }
                else:
                    summary = ctx.graph_svc.rebuild(
                        brain_db_path=str(ctx._data_dir / "brain.db"),
                    )
            finally:
                _indexing_end(project_id)
            summary["refreshed_files"] = indexed
            summary["chunks_processed"] = chunks
            summary["chunk_size"] = chunk_size
            return [TextContent(type="text", text=_json(summary))]

        if name == "prism_cancel_pending":
            in_flight = indexing_in_flight(project_id)
            if in_flight:
                request_cancel(project_id)
                return [TextContent(type="text", text=_json({
                    "cancelled_requested": True,
                    "indexing_in_flight": in_flight,
                }))]
            return [TextContent(type="text", text=_json({
                "cancelled_requested": False,
                "indexing_in_flight": 0,
                "note": "no in-flight refresh to cancel",
            }))]

        if name == "prism_install":
            # Returns the client-side install manifest so the agent can
            # Write the hook files into the user's project directly.
            host_platform = (arguments or {}).get("host_platform")
            return [TextContent(type="text", text=_json(
                _install_manifest(project_id, host_platform)
            ))]

        if name == "prism_sync":
            ctx = get_project(project_id)
            brain_path = str(ctx._data_dir / "brain.db")
            backfilled = ctx.graph_svc.backfill_from_brain(brain_path)
            summary = ctx.graph_svc.rebuild(brain_db_path=brain_path)
            summary["backfilled_via_sync"] = backfilled
            return [TextContent(type="text", text=_json(summary))]

        if name == "prism_guide":
            section = (arguments or {}).get("section", "").strip().lower() or None
            return [TextContent(type="text", text=_prism_guide(section))]

        # ------------------------------------------------------------------
        # Verifier — outer-harness sensor
        # ------------------------------------------------------------------
        if name == "verifier_run":
            ctx = get_project(project_id)
            args = arguments or {}
            result = ctx.verifier_svc.run(
                session_id=args.get("session_id"),
                task_id=args.get("task_id"),
                since_iso=args.get("since_iso"),
                baseline_rev=args.get("baseline_rev"),
                workspace=args.get("workspace"),
            )
            return [TextContent(type="text", text=_json(result))]

        if name == "verifier_history":
            ctx = get_project(project_id)
            args = arguments or {}
            rows = ctx.verifier_svc.history(
                task_id=args.get("task_id"),
                limit=int(args.get("limit", 20)),
            )
            return [TextContent(type="text", text=_json({"runs": rows}))]

        if name == "verifier_feedback_summary":
            ctx = get_project(project_id)
            limit = int((arguments or {}).get("limit", 50))
            seeds = ctx.verifier_svc.feedback_summary(limit=limit)
            return [TextContent(type="text", text=_json({"seeds": seeds}))]

        # ------------------------------------------------------------------
        # Memory tools
        # ------------------------------------------------------------------
        if name == "memory_store":
            result = memory_svc.store(
                domain=arguments["domain"],
                name=arguments["name"],
                description=arguments["description"],
                type=arguments["type"],
                classification=arguments["classification"],
                evidence=arguments.get("evidence"),
                importance=arguments.get("importance", 5),
                memory_type=arguments.get("memory_type", "semantic"),
                adr_status=arguments.get("adr_status", ""),
                supersedes=arguments.get("supersedes", ""),
            )
            # LL-08: when the caller provides a session_id, stamp a
            # memory_meta row so the janitor can later correlate this
            # memory with the session that wrote it. JSONL remains the
            # source of truth for content; memory_meta is a SQL sidecar
            # for queryable metadata only.
            sid = arguments.get("session_id")
            if sid:
                # Accept dict, dataclass, or pydantic-like: memory_svc
                # returns an ExpertiseEntry dataclass today, but keep
                # attribute+mapping lookup so a future shape change
                # doesn't silently drop the stamp.
                mem_id = None
                for attr in ("id", "entry_id", "memory_id"):
                    if isinstance(result, dict):
                        mem_id = result.get(attr)
                    else:
                        mem_id = getattr(result, attr, None)
                    if mem_id:
                        break
                if mem_id:
                    import sqlite3 as _sq3
                    _c = _sq3.connect(str(ctx._data_dir / "scores.db"))
                    try:
                        _c.execute(
                            "INSERT OR REPLACE INTO memory_meta "
                            "(memory_id, session_id, status) "
                            "VALUES (?, ?, 'active')",
                            (mem_id, sid),
                        )
                        _c.commit()
                    finally:
                        _c.close()
            # Phase 2 (epic 4fd1e6b4): announce the durable write on the
            # learning bus AFTER memory_svc.store committed. Best-effort,
            # mirrors the memory_meta side-effect idiom — a bus failure
            # must never break the primary write. Handlers are wrap/no-op
            # this phase (dual-run), so this is observable-only.
            try:
                from prism_service.services import event_pool as _ep
                _mid = None
                for _attr in ("id", "entry_id", "memory_id"):
                    _mid = (result.get(_attr) if isinstance(result, dict)
                            else getattr(result, _attr, None))
                    if _mid:
                        break
                _ep.get_bus().emit(_ep.Event(
                    type=_ep.MEMORY_WRITTEN,
                    payload={"memory_id": _mid},
                ))
            except Exception:
                pass  # best-effort — never break the memory write
            return [TextContent(type="text", text=_json(result))]

        if name == "memory_invalidate":
            import sqlite3 as _sq3
            mem_id = arguments["memory_id"]
            reason = arguments.get("reason", "")
            _c = _sq3.connect(str(ctx._data_dir / "scores.db"))
            try:
                # INSERT OR REPLACE so memories that never had a
                # memory_meta row still get one (invalidated directly
                # without having been session-tagged first).
                _c.execute(
                    "INSERT INTO memory_meta (memory_id, status) "
                    "VALUES (?, 'invalidated') "
                    "ON CONFLICT(memory_id) DO UPDATE SET status='invalidated'",
                    (mem_id,),
                )
                _c.commit()
            finally:
                _c.close()
            return [TextContent(type="text", text=_json({
                "accepted": True, "memory_id": mem_id, "reason": reason,
            }))]

        # ------------------------------------------------------------------
        # LL-08 — Janitor / Layer-B queue endpoints
        # ------------------------------------------------------------------
        if name == "janitor_enqueue":
            cid = ctx.janitor_svc.enqueue(
                task_id=arguments.get("task_id"),
                session_id=arguments.get("session_id"),
                trigger=arguments.get("trigger", "manual"),
                scope=arguments.get("scope"),
            )
            return [TextContent(type="text", text=_json({"candidate_id": cid}))]

        if name == "janitor_mark_stale":
            staled = ctx.janitor_svc.mark_stale(
                session_id=arguments["session_id"],
                scope=arguments.get("scope"),
            )
            return [TextContent(type="text", text=_json({"staled": staled}))]

        if name == "janitor_check":
            res = ctx.janitor_svc.check(session_id=arguments["session_id"])
            return [TextContent(type="text", text=_json(res))]

        if name == "janitor_submit":
            res = ctx.janitor_svc.submit(
                candidate_id=arguments["candidate_id"],
                output_json=arguments["output_json"],
            )
            return [TextContent(type="text", text=_json(res))]

        if name == "janitor_abandon":
            res = ctx.janitor_svc.abandon(
                candidate_id=arguments["candidate_id"],
                reason=arguments.get("reason", ""),
            )
            return [TextContent(type="text", text=_json(res))]

        if name == "janitor_status":
            _db = ctx.janitor_svc._db
            rows = _db.execute(
                "SELECT status, COUNT(*) AS n FROM consolidation_candidates "
                "GROUP BY status"
            ).fetchall()
            counts = {r["status"]: r["n"] for r in rows}
            recent_nudge = _db.execute(
                "SELECT MAX(last_nudged_at) AS ts FROM consolidation_candidates"
            ).fetchone()
            return [TextContent(type="text", text=_json({
                "pending": counts.get("pending", 0),
                "dispensed": counts.get("dispensed", 0),
                "completed": counts.get("completed", 0),
                "abandoned": counts.get("abandoned", 0),
                "stale": counts.get("stale", 0),
                "last_nudge_at": recent_nudge["ts"] if recent_nudge else None,
            }))]

        # ------------------------------------------------------------------
        # Ultimate Graph annotation PULL loop (#50) - durable, {name,purpose}
        # ------------------------------------------------------------------
        if name == "graph_annotate_enqueue":
            n = ctx.graph_annotate_svc.enqueue_project(
                arguments.get("project") or project_id)
            return [TextContent(type="text", text=_json({"enqueued": n}))]

        if name == "graph_annotate_check":
            res = ctx.graph_annotate_svc.check(
                session_id=arguments["session_id"])
            return [TextContent(type="text", text=_json(res))]

        if name == "graph_annotate_submit":
            res = ctx.graph_annotate_svc.submit(
                brief_id=arguments["brief_id"],
                output=arguments.get("output"),
            )
            return [TextContent(type="text", text=_json(res))]

        if name == "graph_annotate_abandon":
            res = ctx.graph_annotate_svc.abandon(
                brief_id=arguments["brief_id"],
                reason=arguments.get("reason", ""),
            )
            return [TextContent(type="text", text=_json(res))]

        if name == "graph_annotate_status":
            return [TextContent(type="text",
                                text=_json(ctx.graph_annotate_svc.status()))]

        if name == "memory_recall":
            results = memory_svc.recall(
                query=arguments["query"],
                domain=arguments.get("domain"),
                limit=arguments.get("limit", 5),
            )
            return [TextContent(type="text", text=_json(results))]

        # ------------------------------------------------------------------
        # OKF tools — read-only projection of memory + brain as an OKF wiki
        # ------------------------------------------------------------------
        if name in ("okf_index", "okf_get", "okf_graph"):
            from prism_service.services.okf_host import OkfHost

            host = OkfHost(memory_svc, brain_svc)
            if name == "okf_index":
                return [TextContent(type="text", text=_json(host.index()))]
            if name == "okf_graph":
                return [TextContent(type="text", text=_json(host.graph()))]
            result = host.get(arguments["path"])
            if result is None:
                return [TextContent(type="text", text=_json({"error": f"unknown concept: {arguments['path']}"}))]
            return [TextContent(type="text", text=_json(result))]

        # ------------------------------------------------------------------
        # Architecture principles — seed machine-checkable layer rules so the
        # conductor's plan_gate is satisfiable on a fresh project (issue #171)
        # ------------------------------------------------------------------
        if name == "principles_seed":
            from prism_service.services.arc_governance import (
                seed_default_principles)
            stored = seed_default_principles(
                memory_svc, rules=arguments.get("rules"))
            return [TextContent(type="text", text=_json({
                "seeded": len(stored),
                "ids": [getattr(e, "name", "") for e in stored],
                "domain": "architecture-principles",
            }))]

        # ------------------------------------------------------------------
        # prism_onboard — one-call bootstrap: seed principles + return the
        # .mcp.json snippet / ports / version / prism_guide pointer (#172)
        # ------------------------------------------------------------------
        if name == "prism_onboard":
            from prism_service import config as _cfg
            from prism_service.services.arc_governance import (
                seed_default_principles)
            stored = seed_default_principles(memory_svc)
            mcp_url = (f"http://localhost:{_cfg.MCP_PORT}/mcp/"
                       f"?project={project_id}")
            mcp_url_all = f"{mcp_url}&tool_profile=all"
            interactive_n = len(tool_names_for_profile("interactive"))
            all_n = len({tool.name for tool in TOOLS})
            return [TextContent(type="text", text=_json({
                "seeded": len(stored),
                "project_id": project_id,
                "prism_version": PRISM_VERSION,
                "mcp_port": _cfg.MCP_PORT,
                "web_port": _cfg.UI_PORT,
                "mcp_url": mcp_url,
                "mcp_url_all": mcp_url_all,
                "mcp_json": {"mcpServers": {"prism": {
                    "type": "http", "url": mcp_url}}},
                "tool_surface": {
                    "default_profile": "interactive",
                    "default_tool_count": interactive_n,
                    "all_tool_count": all_n,
                    "note": (
                        "The default profile is a small curated surface. "
                        "Maintenance/admin + legacy endpoints (brain_index_doc, "
                        "graph_rebuild, project_create, verifier_run, the legacy "
                        "understand_* family, …) are hidden by default — connect "
                        "with mcp_url_all (?tool_profile=all) to use them."
                    ),
                },
                # How an already-connected agent picks up NEW MCP endpoints after
                # PRISM ships a new version (the daemon auto-updates / `prism
                # update`). The client caches the tool list at connect time, so a
                # reconnect is required — this tells the agent exactly how.
                "staying_current": [
                    f"You onboarded on PRISM v{PRISM_VERSION}. Your MCP client "
                    "fetched the tool list ONCE at connect time and cached it — "
                    "new endpoints from a later PRISM version will NOT appear "
                    "until you reconnect.",
                    "To pick up new MCP endpoints: in Claude Code run `/mcp` and "
                    "reconnect the `prism` server (or restart Claude Code); other "
                    "MCP clients re-open the session. Then call prism_guide again.",
                    "Detect drift: call prism_status and compare its "
                    "`prism_version` to the version you onboarded on. If it moved, "
                    "reconnect to refresh the tool list.",
                    "After a PRISM version change also re-run prism_install "
                    "(idempotent) to heal client-side hook/script drift, then "
                    "restart Claude so the refreshed hooks AND tool list load.",
                    "To reach maintenance/admin or legacy endpoints not in the "
                    f"default surface, reconnect using mcp_url_all: {mcp_url_all}",
                ],
                # How-to-use highlights so an onboarding agent doesn't just see
                # the tool list — it learns the gate + lean-response patterns
                # up front (full detail in prism_guide('orchestration')).
                "best_practices": [
                    "DECLARE proof_type per task so red/green gates check the "
                    "RIGHT oracle instead of always demanding a failing test: "
                    "test=TDD (default), metric=a count-delta receipt in "
                    "completion_proof (no red test needed — proves an already-"
                    "green fix), artifact=a path, demo=a UI screenshot. A "
                    "ui-tagged task with a non-demo proof_type is judged on "
                    "that shape, not force-required to be a screenshot.",
                    "AUTHOR to the rubric, don't discover it by failing: "
                    "conductor_advance INTO draft_story/verify_plan returns a "
                    "`rubric` (required sections, AC-<n> id pattern, 'oracle:' "
                    "marker) — shape plan_doc to match BEFORE approving.",
                    "Keep conductor responses LEAN: pass "
                    "fields=['from_step','to_step','gate_state'] to "
                    "conductor_advance/conductor_gate (omits the echoed task), "
                    "and parent_id to task_list to scope to one epic's children.",
                ],
                "next": ("Call prism_guide first — it returns the live "
                         "orientation + the max-fan-out task playbook."),
                "web_ui": f"http://localhost:{_cfg.UI_PORT}/",
            }))]

        # ------------------------------------------------------------------
        # Task tools
        # ------------------------------------------------------------------
        if name == "task_create":
            task = task_svc.create(
                title=arguments["title"],
                description=arguments.get("description", ""),
                priority=arguments.get("priority", 0),
                dependencies=arguments.get("dependencies"),
                tags=arguments.get("tags"),
                story_file=arguments.get("story_file", ""),
                assigned_agent=arguments.get("assigned_agent", ""),
                parent_id=arguments.get("parent_id", ""),
                oracle=arguments.get("oracle", ""),
                proof_type=arguments.get("proof_type", ""),
                completion_proof=arguments.get("completion_proof", ""),
                likely_misfire=arguments.get("likely_misfire", ""),
                full_outcome_complete=bool(arguments.get("full_outcome_complete", False)),
                allowed_files=arguments.get("allowed_files"),
                verify=arguments.get("verify"),
                stop_if=arguments.get("stop_if"),
                plan_doc=arguments.get("plan_doc", ""),
                plan_diagram=arguments.get("plan_diagram", ""),
            )
            return [TextContent(type="text", text=_json(task))]

        if name == "task_list":
            tasks = task_svc.list(
                status=arguments.get("status"),
                assigned_agent=arguments.get("assigned_agent"),
                tag=arguments.get("tag"),
                story_file=arguments.get("story_file"),
                parent_id=arguments.get("parent_id"),
            )
            # FR-7: optional per-task field projection for a lean response.
            _fields = arguments.get("fields")
            if _fields:
                rows = [_serialise(t) for t in tasks]
                projected = [{k: r.get(k) for k in _fields} for r in rows]
                return [TextContent(type="text", text=_json(projected))]
            return [TextContent(type="text", text=_json(tasks))]

        if name == "task_next":
            result = task_svc.next_task()
            if result is None:
                return [TextContent(type="text", text=_json({"task": None, "reason": "No unblocked pending tasks"}))]
            return [TextContent(type="text", text=_json(result))]

        if name == "task_update":
            update_kwargs: dict[str, Any] = {}
            for key in ("title", "status", "priority", "assigned_agent", "blocked_reason", "parent_id", "oracle", "proof_type", "completion_proof", "likely_misfire", "full_outcome_complete", "allowed_files", "verify", "stop_if", "plan_doc", "plan_diagram"):
                if key in arguments:
                    update_kwargs[key] = arguments[key]
            task = task_svc.update(arguments["id"], **update_kwargs)
            if task is None:
                return [TextContent(type="text", text=_json({"error": f"Task {arguments['id']} not found"}))]

            # Learning loop: correlate task outcome with recalled memories
            new_status = arguments.get("status", "")
            if new_status in ("done", "blocked"):
                outcome = "positive" if new_status == "done" else "negative"
                try:
                    _updated = memory_svc.record_outcome(arguments["id"], outcome)
                except Exception:
                    _updated = 0  # best-effort — don't break task updates
                # Phase 2 (epic 4fd1e6b4): emit memory.recalled+outcome ONLY
                # when an outcome was actually attached (record_outcome updated
                # >0 recall_log rows). Best-effort; handlers are wrap/no-op.
                if _updated and _updated > 0:
                    try:
                        from prism_service.services import event_pool as _ep
                        _ep.get_bus().emit(_ep.Event(
                            type=_ep.MEMORY_RECALLED_OUTCOME,
                            payload={
                                "task_id": arguments["id"],
                                "outcome": outcome,
                                "updated": _updated,
                            },
                        ))
                    except Exception:
                        pass  # best-effort — never break task updates

            return [TextContent(type="text", text=_json(task))]

        if name == "task_link_session":
            # FORCED WRITER. session_id resolution: caller-passed wins;
            # when omitted, fall back to the MCP request_id as the
            # server-inferred session handle (no thread-locals).
            tid = str(arguments["task_id"])
            sid = arguments.get("session_id")
            if not sid:
                from prism_service.mcp.request_context import get_request_context
                sid = get_request_context().request_id
            if not sid:
                return [TextContent(type="text", text=_json({
                    "ok": False, "task_id": tid,
                    "reason": "no session_id supplied and none inferable",
                }))]
            ok = task_svc.link_session(tid, str(sid))
            return [TextContent(type="text", text=_json({
                "ok": ok, "task_id": tid, "session_id": str(sid),
            }))]

        # ------------------------------------------------------------------
        # Workflow tools
        # ------------------------------------------------------------------
        if name == "workflow_state":
            state = workflow_svc.get_state()
            return [TextContent(type="text", text=_json(state))]

        if name == "workflow_advance":
            result = workflow_svc.advance(
                validation=arguments.get("validation"),
                gate_action=arguments.get("gate_action"),
            )
            return [TextContent(type="text", text=_json(result))]

        # ------------------------------------------------------------------
        # Conductor v2 — per-task state machine
        # ------------------------------------------------------------------
        if name == "conductor_advance":
            task_id = arguments["id"]
            # Session capture: prefer the caller-threaded session_id (the human
            # driving session a workflow passes as SID); else resolve the REAL
            # active transcript session so the linked id maps to actual token
            # data. The MCP request handle is the last-resort stamp only — on
            # its own it links a phantom (no transcript, no tokens → 0 tok).
            _sid = arguments.get("session_id") or _resolve_link_session_id()
            result = conductor_svc.advance_task(
                task_id,
                validation=arguments.get("validation"),
                session_id=_sid,
            )
            # FR-5: surface the active rubric schema on the authoring step so
            # the author sees the AC-id / oracle / required-section format
            # BEFORE story_gate/plan_gate scores it (no fail-then-replan loop).
            try:
                from prism_service.models.workflow import WORKFLOW_STEPS
                from prism_service.services.arc_governance import load_rubrics
                _to = result.get("to_step")
                _val = next((s.get("validation") for s in WORKFLOW_STEPS
                             if s["id"] == _to), None)
                if _val:
                    _rub = load_rubrics().get(_val)
                    if _rub:
                        result["rubric"] = _rub
            except Exception:
                pass  # advisory — never break an advance
            # FR-7: optional field projection — return only requested keys
            # and OMIT the full task object (lean response).
            _fields = arguments.get("fields")
            if _fields:
                result = {k: result.get(k) for k in _fields}
            else:
                result["task"] = task_svc.get(task_id)
            return [TextContent(type="text", text=_json(result))]

        if name == "conductor_gate":
            task_id = arguments["id"]
            # Same session-capture fallback as conductor_advance — resolve the
            # real active transcript session (not the phantom request handle) so
            # the terminal gate links a session that carries token data.
            _sid = arguments.get("session_id") or _resolve_link_session_id()
            # actor = who is clearing the gate; defaults to the linking
            # session so the NO-SELF-OVERRIDE guard can compare it against
            # the work-producing sessions (task 3826dac3).
            _actor = arguments.get("actor") or _sid
            result = conductor_svc.gate_decide(
                task_id,
                arguments["action"],
                reason=arguments.get("reason", ""),
                override=bool(arguments.get("override", False)),
                session_id=_sid,
                actor=_actor,
            )
            # FR-7: optional field projection — return only requested keys
            # and OMIT the full task object (lean response).
            _fields = arguments.get("fields")
            if _fields:
                result = {k: result.get(k) for k in _fields}
            else:
                result["task"] = task_svc.get(task_id)
            return [TextContent(type="text", text=_json(result))]

        # ------------------------------------------------------------------
        # Context bundle
        # ------------------------------------------------------------------
        if name == "context_bundle":
            from prism_service.mcp.request_context import get_request_context
            from prism_service.services.context_builder import ContextBuilder

            request_ctx = get_request_context()
            bundle = ContextBuilder(
                project_id=project_id,
                brain_svc=brain_svc,
                memory_svc=memory_svc,
                task_svc=task_svc,
                workflow_svc=workflow_svc,
                governance=governance,
                request_id=request_ctx.request_id,
            ).build(
                persona=arguments.get("persona"),
                story_file=arguments.get("story_file"),
            )
            return [TextContent(type="text", text=_json(bundle))]

        # ------------------------------------------------------------------
        # v5.1 understand-anything surface (sidecar dispatcher)
        # ------------------------------------------------------------------
        _u = _understand_dispatch(name, arguments, project_id)
        if _u is not None:
            return _u

        # ------------------------------------------------------------------
        # Unknown tool
        # ------------------------------------------------------------------
        return [TextContent(type="text", text=f"Error: Unknown tool '{name}'")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")]
