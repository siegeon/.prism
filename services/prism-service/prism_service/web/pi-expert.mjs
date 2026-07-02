/**
 * pi-expert — PI's PRISM expertise, in ONE place (task e70cdcda).
 *
 * The single source of (a) the expert system prompt and (b) the full
 * PRISM tool catalog (TypeBox schemas matched to the real MCP
 * inputSchemas in prism_service/mcp/tools.py). Imported by BOTH
 * surfaces: web/pi-runtime.mjs (Node subprocess) and the SPA rail panel
 * (src/lib/piAgent.ts) — never duplicate a prompt or schema per surface.
 *
 * Lives in web/ so the bare "@earendil-works/pi-ai" import resolves from
 * the SPA's node_modules in Node AND Vite.
 */

import { Type } from "@earendil-works/pi-ai";

// ------------------------------------------------------------- prompt

export const EXPERT_SYSTEM_PROMPT = `You are PI, PRISM's expert agent. PRISM is an AI-assisted engineering system with four stores you operate through tools:
- Brain: a hybrid (BM25 + vector + graph) index of the project's code and docs, plus a code graph (symbols, call edges, references).
- Memory: the team's long-term memory — conventions, decisions, failures, patterns — recallable by natural language.
- Tasks: a tracker (tasks, epics via parent_id, tags, oracle/proof fields).
- Conductor: a per-task SDLC state machine that walks every task through the same 10 steps, in order:
  review_previous_notes -> draft_story -> story_gate -> verify_plan -> plan_gate -> write_failing_tests -> red_gate -> implement_tasks -> verify_green_state -> green_gate.

GATE DOCTRINE (non-negotiable):
- conductor_advance moves a task to the NEXT step and is refused while a gate is pending — decide the gate first with conductor_gate.
- conductor_gate action='approve' REQUIRES a reason describing the validation EVIDENCE used (test run output, rubric result, artifact path). Approving without evidence is forbidden.
- story_gate and plan_gate are rubric-verified mechanically over the task's plan_doc: it needs '## Summary', '## Requirements' with FR-n bullets, and '## Acceptance Criteria' with AC-n bullets where EVERY AC line carries an inline '- oracle: <observable check>'. plan_diagram must be RAW Mermaid source starting with a diagram keyword (flowchart, sequenceDiagram, classDiagram, stateDiagram, erDiagram...). Attach plan_doc + plan_diagram (task_update) BEFORE advancing into story_gate.
- red_gate expects a committed failing-test trace; green_gate expects captured full-suite-green or a demonstrable-UI artifact.
- A FAILED gate LATCHES: do not try to advance past it and do not retry-approve in a loop — report the failure and its reason instead.

TASK DISCIPLINE:
- Titles are short, human-friendly statements of WHAT the feature is (~4-9 words). Mechanics, phases and file lists go in the description, never the title.
- Set an oracle (the observable signal that proves the outcome) and a proof_type matched to it: test (a suite), demo (a shown surface), artifact (a file path), metric (a number moving). completion_proof must carry receipt-backed evidence, not self-attestation.

MEMORY DISCIPLINE:
- ALWAYS memory_recall before memory_store — never store a duplicate of existing knowledge.
- A memory without evidence is nearly useless: include concrete file paths / code references in the description.

TOOL ECONOMY:
- You run on a small model: make ONE well-aimed tool call instead of many exploratory ones. Prefer brain_understand for orientation, brain_find_symbol/brain_outline for targeted reads.
- NEVER fabricate a tool result, test output, or gate evidence. If you do not have the evidence, say so.`;

// ------------------------------------------------------------ catalog

// Keyed by the REAL MCP tool names dispatched by mcp.tools.handle_tool;
// every parameter name below is verified against the matching
// inputSchema in prism_service/mcp/tools.py. Additions/removals are a
// deliberate decision mirrored in api/agent.py's AGENT_TOOL_WHITELIST.
export const EXPERT_TOOL_DEFS = {
  // ---------------------------------------------------------- brain
  brain_search: {
    label: "Brain search",
    description: "Search the project's code/knowledge base (hybrid BM25 + vector + graph). Use for keyword/concept lookups when you don't yet know which file or symbol matters.",
    parameters: Type.Object({
      query: Type.String({ description: "Search query" }),
      domain: Type.Optional(Type.String({ description: "Filter by domain (py, ts, md, expertise)" })),
      limit: Type.Optional(Type.Integer({ description: "Max results (default 5)" })),
      domains: Type.Optional(Type.Array(Type.String(), { description: "Filter by multiple domains" })),
    }),
  },
  brain_understand: {
    label: "Brain understand",
    description: "ONE graph-aware retrieval: ranked hits + their neighbor subgraph + per-file context bundles. Use FIRST when orienting on an area of the codebase — it replaces chaining brain_search + references + call chains. Empty query = whole-graph overview.",
    parameters: Type.Object({
      query: Type.Optional(Type.String({ description: "What to understand; omit for a centrality-ranked overview" })),
      limit: Type.Optional(Type.Integer({ description: "Max ranked hits (default 20)" })),
      depth: Type.Optional(Type.Integer({ description: "Neighbor hops 0-3 (default 1)" })),
      seed_files: Type.Optional(Type.Array(Type.String(), { description: "Seed the view from these files instead of a text query" })),
      label: Type.Optional(Type.String({ description: "Human label for a seed_files selection" })),
    }),
  },
  brain_find_symbol: {
    label: "Find symbol",
    description: "Fetch the chunk(s) for a named function/class/method — the token-cheap alternative to reading a whole file. Use when you know the symbol name.",
    parameters: Type.Object({
      name: Type.String({ description: "Entity name (function, class, method)" }),
      kind: Type.Optional(Type.String({ description: "Optional filter: function|class|method|module" })),
      limit: Type.Optional(Type.Integer({ description: "Max chunks (default 10)" })),
    }),
  },
  brain_outline: {
    label: "File outline",
    description: "Symbol outline of a source file (names/kinds/line ranges, no bodies). Use to orient on a file BEFORE fetching specific chunks via brain_find_symbol.",
    parameters: Type.Object({
      source_file: Type.String({ description: "File path as indexed" }),
    }),
  },
  brain_find_references: {
    label: "Find references",
    description: "Call sites of a named entity via the code graph — semantic 'who uses this'. Use before changing a function to see its blast surface.",
    parameters: Type.Object({
      name: Type.String({ description: "Entity name" }),
      limit: Type.Optional(Type.Integer({ description: "Max results (default 20)" })),
      include_rationale: Type.Optional(Type.Boolean({ description: "Include rationale-comment edges (default false)" })),
    }),
  },
  brain_call_chain: {
    label: "Call chain",
    description: "Bounded BFS over the call graph from an entity. direction='callers' answers 'who breaks if I change this?'; 'callees' traces forward call flow.",
    parameters: Type.Object({
      entity: Type.String({ description: "Start entity" }),
      depth: Type.Optional(Type.Integer({ description: "Max hops (default 2)" })),
      limit: Type.Optional(Type.Integer({ description: "Max edges (default 50)" })),
      relation: Type.Optional(Type.String({ description: "Edge-kind filter: 'calls' (default), '*', 'uses', 'inherits'..." })),
      direction: Type.Optional(Type.String({ description: "callees | callers | both" })),
    }),
  },
  // --------------------------------------------------------- memory
  memory_recall: {
    label: "Memory recall",
    description: "Recall the team's long-term memory: conventions, decisions, failures. ALWAYS check here before claiming something is new knowledge and before every memory_store.",
    parameters: Type.Object({
      query: Type.String({ description: "Natural-language query" }),
      domain: Type.Optional(Type.String({ description: "Filter by domain" })),
      limit: Type.Optional(Type.Integer({ description: "Max results (default 5)" })),
    }),
  },
  memory_store: {
    label: "Memory store",
    description: "Store an expertise entry in long-term memory. Use ONLY after memory_recall shows no duplicate; the description must carry file-path/code evidence.",
    parameters: Type.Object({
      domain: Type.String({ description: "Expertise domain (conventions, architecture, testing...)" }),
      name: Type.String({ description: "Short kebab-case name" }),
      description: Type.String({ description: "DETAILED description with file paths / code examples" }),
      type: Type.String({ description: "pattern | convention | failure | decision" }),
      classification: Type.String({ description: "tactical | foundational | strategic" }),
      evidence: Type.Optional(Type.Object({}, { additionalProperties: true, description: "{file_paths: [...], commit, pr}" })),
      importance: Type.Optional(Type.Integer({ description: "1-10 (default 5)" })),
      memory_type: Type.Optional(Type.String({ description: "semantic | episodic | procedural" })),
      adr_status: Type.Optional(Type.String({ description: "ADR status for type=decision: proposed | accepted | superseded" })),
      supersedes: Type.Optional(Type.String({ description: "mx-XXXXXX id this entry supersedes" })),
      session_id: Type.Optional(Type.String({ description: "Session to stamp on the memory row" })),
    }),
  },
  memory_invalidate: {
    label: "Memory invalidate",
    description: "Soft-delete a memory (status=invalidated, audit-preserved). Use when recall surfaces an entry that no longer applies — never leave stale knowledge active.",
    parameters: Type.Object({
      memory_id: Type.String({ description: "Memory id (mx-XXXXXX)" }),
      reason: Type.Optional(Type.String({ description: "Why it no longer applies" })),
    }),
  },
  // ---------------------------------------------------------- tasks
  task_list: {
    label: "Task list",
    description: "List tasks. Default = ACTIVE work only. Use id=<task> to re-read ONE task cheaply (never pull the whole board for that) and fields=[...] to project only the keys you need.",
    parameters: Type.Object({
      status: Type.Optional(Type.String({ description: "pending | in_progress | blocked | done | cancelled | deleted | all" })),
      assigned_agent: Type.Optional(Type.String({ description: "Filter by assigned agent" })),
      tag: Type.Optional(Type.String({ description: "Filter by tag" })),
      story_file: Type.Optional(Type.String({ description: "Filter by story file" })),
      parent_id: Type.Optional(Type.String({ description: "One epic's children; '' for roots only" })),
      id: Type.Optional(Type.String({ description: "By-id read: return just this task" })),
      fields: Type.Optional(Type.Array(Type.String(), { description: "Projection: only these keys per task" })),
    }),
  },
  task_next: {
    label: "Task next",
    description: "Get the next highest-priority unblocked task. Use when asked 'what should be worked on next?'.",
    parameters: Type.Object({}),
  },
  task_create: {
    label: "Task create",
    description: "Create a task. Title = short human-friendly WHAT (~4-9 words, mechanics go in description); set oracle + proof_type up front.",
    parameters: Type.Object({
      title: Type.String({ description: "Short human-friendly title (~4-9 words)" }),
      description: Type.Optional(Type.String({ description: "Mechanics, phases, file hooks — the details" })),
      priority: Type.Optional(Type.Integer({ description: "Higher = more important (default 0)" })),
      dependencies: Type.Optional(Type.Array(Type.String(), { description: "Task IDs this depends on" })),
      tags: Type.Optional(Type.Array(Type.String(), { description: "Tags for categorization" })),
      story_file: Type.Optional(Type.String({ description: "Associated story file path" })),
      assigned_agent: Type.Optional(Type.String({ description: "Persona to assign (sm, dev, qa)" })),
      parent_id: Type.Optional(Type.String({ description: "Parent epic id (makes this a child)" })),
      oracle: Type.Optional(Type.String({ description: "Observable signal that proves the outcome" })),
      proof_type: Type.Optional(Type.String({ description: "test | demo | artifact | metric | review | source_backed_answer | decision" })),
      completion_proof: Type.Optional(Type.String({ description: "Receipt-backed evidence the oracle is satisfied" })),
      likely_misfire: Type.Optional(Type.String({ description: "How this could pass-but-be-wrong" })),
      allowed_files: Type.Optional(Type.Array(Type.String(), { description: "Worker contract: file allowlist" })),
      verify: Type.Optional(Type.Array(Type.String(), { description: "Worker contract: commands that prove the slice" })),
      stop_if: Type.Optional(Type.Array(Type.String(), { description: "Worker contract: halt conditions" })),
      plan_doc: Type.Optional(Type.String({ description: "Plan markdown (## Summary / ## Requirements FR-n / ## Acceptance Criteria, AC-n each with inline '- oracle:')" })),
      plan_diagram: Type.Optional(Type.String({ description: "Raw Mermaid source starting with a diagram keyword" })),
    }),
  },
  task_update: {
    label: "Task update",
    description: "Update a task you are driving: author/patch plan_doc (markdown) or plan_diagram (Mermaid), set status, oracle, or completion_proof. Attach plan_doc BEFORE advancing into story_gate.",
    parameters: Type.Object({
      id: Type.String({ description: "Task ID to update" }),
      title: Type.Optional(Type.String({ description: "Rename (short human-friendly, ~4-9 words)" })),
      status: Type.Optional(Type.String({ description: "pending | in_progress | done | blocked" })),
      priority: Type.Optional(Type.Integer({ description: "New priority" })),
      assigned_agent: Type.Optional(Type.String({ description: "New agent assignment" })),
      blocked_reason: Type.Optional(Type.String({ description: "Reason when status=blocked" })),
      parent_id: Type.Optional(Type.String({ description: "Re-parent under an epic ('' = root)" })),
      oracle: Type.Optional(Type.String({ description: "Observable signal that proves the outcome" })),
      proof_type: Type.Optional(Type.String({ description: "test | demo | artifact | metric | review | source_backed_answer | decision" })),
      completion_proof: Type.Optional(Type.String({ description: "Receipt-backed evidence the oracle is satisfied" })),
      likely_misfire: Type.Optional(Type.String({ description: "How this could pass-but-be-wrong" })),
      allowed_files: Type.Optional(Type.Array(Type.String(), { description: "Worker contract: file allowlist" })),
      verify: Type.Optional(Type.Array(Type.String(), { description: "Worker contract: verify commands" })),
      stop_if: Type.Optional(Type.Array(Type.String(), { description: "Worker contract: halt conditions" })),
      plan_doc: Type.Optional(Type.String({ description: "Plan markdown (## Summary / ## Requirements FR-n / ## Acceptance Criteria, AC-n each with inline '- oracle:')" })),
      plan_diagram: Type.Optional(Type.String({ description: "Raw Mermaid source starting with a diagram keyword" })),
    }),
  },
  // ------------------------------------------------------ conductor
  conductor_advance: {
    label: "Conductor advance",
    description: "Advance a task to the next SDLC workflow step. Refused while a gate is pending — decide the gate first with conductor_gate. Pass a validation note describing what the finished step proved.",
    parameters: Type.Object({
      id: Type.String({ description: "Task ID to advance" }),
      validation: Type.Optional(Type.String({ description: "Validation note for the transition" })),
      session_id: Type.Optional(Type.String({ description: "Session to link on advance" })),
      fields: Type.Optional(Type.Array(Type.String(), { description: "Projection: only these response keys" })),
    }),
  },
  conductor_gate: {
    label: "Conductor gate",
    description: "Decide a pending gate. action 'approve' REQUIRES reason describing the validation evidence used; 'reject' stores reason as the failure. A failed gate LATCHES — report, don't loop.",
    parameters: Type.Object({
      id: Type.String({ description: "Task ID whose gate to decide" }),
      action: Type.String({ description: "approve | reject" }),
      reason: Type.String({ description: "Validation evidence (required on approve)" }),
      override: Type.Optional(Type.Boolean({ description: "Bypass verifier / recover a failed gate — distinct actor only, audited" })),
      actor: Type.Optional(Type.String({ description: "Who clears the gate (must differ from the work-producing actor on override)" })),
      session_id: Type.Optional(Type.String({ description: "Session to link on the decision" })),
      fields: Type.Optional(Type.Array(Type.String(), { description: "Projection: only these response keys" })),
    }),
  },
  workflow_state: {
    label: "Workflow state",
    description: "Current PRISM workflow state (active step, progress, session info). Use to orient before advancing anything.",
    parameters: Type.Object({}),
  },
  // -------------------------------------------------------- context
  context_bundle: {
    label: "Context bundle",
    description: "Deterministic context pack: role card, rules, Brain context, memory recall, active tasks, workflow state, health. Use at session start or when handed a persona.",
    parameters: Type.Object({
      persona: Type.Optional(Type.String({ description: "Agent persona (sm, dev, qa)" })),
      story_file: Type.Optional(Type.String({ description: "Story file path for scoped context" })),
    }),
  },
  prism_status: {
    label: "PRISM status",
    description: "Brain/Graph index health (doc counts, staleness). Use to check whether the index is trustworthy before leaning on Brain results.",
    parameters: Type.Object({
      file_hashes: Type.Optional(Type.Object({}, { additionalProperties: true, description: "Optional {path: sha256} map for precise drift detection" })),
    }),
  },
};
