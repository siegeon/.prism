export const meta = {
  name: 'prototype',
  description: 'PRISM-first research → source fallback → synthesize plan → register as a conductor-tracked planning task. The engine behind the /prototype phase router.',
  whenToUse: 'Run when starting a feature/phase router: mines PRISM knowledge first, fills gaps from source code, synthesizes a PRD-style plan, and parks the work in conductor\'s planning steps.',
  phases: [
    { title: 'Recall', detail: 'Mine PRISM brain + memory in parallel' },
    { title: 'Coverage', detail: 'Judge what PRISM answered; list gaps' },
    { title: 'Source dive', detail: 'One agent per gap greps/reads real source' },
    { title: 'Plan', detail: 'Synthesize PRD-style plan from all evidence' },
    { title: 'Track', detail: 'Create task + walk it through conductor planning steps' },
  ],
}

// `args` can arrive as a JSON string (the harness serializes objects to a
// string before the script sees them), a plain object, or a bare feature
// string. Normalize all three.
let _input = args
if (typeof _input === 'string') {
  try { _input = JSON.parse(_input) } catch { _input = { feature: _input } }
}
const feature = (_input && typeof _input === 'object' ? _input.feature : _input) || ''
// The DRIVING Claude session id — passed via args by the orchestrator
// (workflow JS has no env/process access). GUARDED for the bare-string args
// case: only read session_id when _input is an object. Empty SID => today's
// behavior (never emit task_link_session / session_id with an empty value).
const SID = (_input && typeof _input === 'object' ? (_input.session_id || '') : '').trim()

// Fail fast: never spawn the fleet or create a tracked task for a placeholder.
if (!feature || /^unspecified feature/i.test(feature)) {
  throw new Error('prototype workflow: no feature supplied. Invoke as Workflow({name:"prototype", args:{feature:"<what to plan>"}}). Refusing to plan a placeholder.')
}

const RECALL_SCHEMA = {
  type: 'object',
  required: ['findings', 'answered_aspects', 'confidence'],
  properties: {
    findings: { type: 'array', items: { type: 'object', required: ['source', 'summary'],
      properties: { source: { type: 'string' }, summary: { type: 'string' } } } },
    answered_aspects: { type: 'array', items: { type: 'string' } },
    confidence: { type: 'number', description: '0-1 how well PRISM covers this feature' },
  },
}

const COVERAGE_SCHEMA = {
  type: 'object',
  required: ['covered', 'gaps', 'need_source_dive'],
  properties: {
    covered: { type: 'array', items: { type: 'string' } },
    gaps: { type: 'array', items: { type: 'string', description: 'a specific unanswered question to chase in source' } },
    need_source_dive: { type: 'boolean' },
  },
}

const SOURCE_SCHEMA = {
  type: 'object',
  required: ['gap', 'files', 'findings'],
  properties: {
    gap: { type: 'string' },
    files: { type: 'array', items: { type: 'object', required: ['path', 'why'],
      properties: { path: { type: 'string' }, why: { type: 'string' } } } },
    findings: { type: 'string', description: 'what the source actually shows, with file:line refs' },
  },
}

const PLAN_SCHEMA = {
  type: 'object',
  required: ['title', 'summary', 'actors', 'views', 'constraints', 'journeys', 'plan_steps', 'open_questions', 'plan_diagram', 'plan_doc'],
  properties: {
    // TITLE DIRECTIVE: the title is a SHORT, HUMAN-FRIENDLY statement of WHAT
    // the feature is — what a user would recognize it as. ~4-9 words, plain
    // language. NO mechanics, NO phase/step enumerations, NO data-hook/file
    // names, NO arrow-diagrams, NO "(initializing -> ... -> gates)" jargon.
    // ALL of that belongs in `summary`/description, never the title.
    // Good: "Animate the conductor task tile through its lifecycle".
    // Bad:  "Animate conductor task lifecycle (initializing -> live turns ->
    //        gates) + sub-agent lanes in lock step".
    title: { type: 'string', description: 'SHORT human-friendly feature title (~4-9 words, plain language): what the feature IS, as a user would name it. No mechanics, phase lists, arrow-diagrams, file/data-hook names, or jargon — those go in summary.' },
    summary: { type: 'string', description: 'The detailed description: mechanics, phases, data hooks, file:line seams, acceptance. This is where everything the title omits lives.' },
    actors: { type: 'array', items: { type: 'string' } },
    views: { type: 'array', items: { type: 'string' } },
    constraints: { type: 'array', items: { type: 'string' } },
    journeys: { type: 'array', items: { type: 'string' } },
    plan_steps: { type: 'array', items: { type: 'string' } },
    open_questions: { type: 'array', items: { type: 'string' } },
    // Rich-plan rendering (task a69d30dd): a Mermaid sequence/UML diagram
    // + a markdown proposed-change doc, persisted onto the task so PRISM
    // renders the plan as a document (diagram on top, proposed change below).
    plan_diagram: { type: 'string', description: 'Mermaid source (sequenceDiagram / classDiagram / flowchart) for the plan diagram. Consult the mermaid-syntax skill for valid syntax + Hermes theming — the plan_coverage rubric requires it to parse.' },
    plan_doc: { type: 'string', description: 'Markdown proposed-change/story doc rendered below the diagram. MUST carry the rubric-required sections (## Summary, ## Requirements, ## Acceptance Criteria) with requirement ids: FR-<n>/NFR-<n> bullets under Requirements and AC-<n> bullets under Acceptance Criteria, each AC ending in "— oracle: <observable check>". The story_gate/plan_gate rubrics score these mechanically.' },
  },
}

const TRACK_SCHEMA = {
  type: 'object',
  required: ['task_id', 'current_step', 'steps_walked', 'notes'],
  properties: {
    task_id: { type: 'string' },
    current_step: { type: 'string' },
    steps_walked: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
}

const PRISM_HINT = 'You have PRISM MCP tools available via ToolSearch. Load them first: ToolSearch("select:mcp__prism__brain_search,mcp__prism__memory_recall,mcp__prism__brain_understand"). Project is "prism".'

// ── Phase 1: Recall (parallel mine of brain + memory) ──────────────────
phase('Recall')
const [brain, mem] = await parallel([
  () => agent(
    `${PRISM_HINT}\nUse brain_search (try 3-4 query variants) and brain_understand to find everything PRISM knows relevant to building this feature:\n\n"${feature}"\n\nFocus on existing skills, conductor/workflow plumbing, and any prior phase-router or PRD patterns. Return findings with source files and a confidence score for how well PRISM covers it.`,
    { label: 'recall:brain', phase: 'Recall', schema: RECALL_SCHEMA }),
  () => agent(
    `${PRISM_HINT}\nUse memory_recall (try 3-4 query variants) to recall conventions, decisions, and expertise relevant to:\n\n"${feature}"\n\nReturn findings with source and a confidence score.`,
    { label: 'recall:memory', phase: 'Recall', schema: RECALL_SCHEMA }),
])

const recallBlob = JSON.stringify({ brain, mem }, null, 2)

// ── Phase 2: Coverage judgement ────────────────────────────────────────
phase('Coverage')
const coverage = await agent(
  `Given this PRISM recall result for the feature "${feature}":\n\n${recallBlob}\n\nDecide what is genuinely ANSWERED vs what is still a GAP that requires reading actual source code. Be strict: a vague mention is not coverage. List each gap as a specific question. Set need_source_dive=true if any gap requires source inspection.`,
  { label: 'coverage:judge', phase: 'Coverage', schema: COVERAGE_SCHEMA })

// ── Phase 3: Source dive — one agent per gap, only if needed ────────────
let sourceFindings = []
if (coverage.need_source_dive && coverage.gaps.length) {
  phase('Source dive')
  log(`PRISM left ${coverage.gaps.length} gap(s) — diving into source`)
  sourceFindings = (await parallel(coverage.gaps.slice(0, 6).map((gap) => () =>
    agent(
      `PRISM did not answer this gap for the feature "${feature}":\n\n"${gap}"\n\nResearch the REAL source under E:/.prism (use Grep, Glob, Read, and ToolSearch->mcp__prism__brain_call_chain if you need call flow). Cite concrete file:line. If the answer truly isn't in source either, say so explicitly.`,
      { label: `source:${gap.slice(0, 30)}`, phase: 'Source dive', schema: SOURCE_SCHEMA }))
  )).filter(Boolean)
} else {
  log('PRISM coverage sufficient — skipping source dive')
}

// ── Phase 4: Synthesize the plan / PRD ─────────────────────────────────
phase('Plan')
const evidence = JSON.stringify({ recall: { brain, mem }, coverage, sourceFindings }, null, 2)
const plan = await agent(
  `Synthesize a PRD-style plan for the feature "${feature}" using ONLY the evidence below — do not invent capabilities the evidence doesn't support. Where evidence is thin, put it in open_questions rather than asserting it.\n\nAlso emit TWO rich-render fields:\n- plan_diagram: a valid Mermaid diagram (sequenceDiagram for actor↔system journeys, or classDiagram/flowchart for structure) capturing the core flow of this change. CONSULT THE mermaid-syntax SKILL (.claude/skills/mermaid-syntax) for valid syntax, the 6 supported diagram types, c4 layout science, and Hermes theming — the conductor's plan_coverage rubric mechanically requires plan_diagram to be present AND parse. Raw Mermaid source only — no code fences.\n- plan_doc: a markdown PROPOSED-CHANGE/STORY doc with the rubric-required sections — ## Summary, ## Requirements (FR-<n>/NFR-<n> bullet ids), ## Acceptance Criteria (AC-<n> bullet ids, each ending "— oracle: <observable check>") — plus plan steps and open questions. No raw JSON, no <pre>. The story_gate rubric scores these sections mechanically.\n\nEVIDENCE:\n${evidence}`,
  { label: 'plan:synthesize', phase: 'Plan', schema: PLAN_SCHEMA })

// ── Phase-merge gate doctrine (stacked-epic stranding fix, task 56458db1) ──
// The implement workflow cuts EVERY task branch off origin/main, so a
// stacked multi-phase epic's phase N+1 depends on un-merged phase N
// substrate and hard-halts (memory project_implement_branches_off_main).
// The phase router MUST therefore enforce a PHASE-MERGE GATE: when this
// plan decomposes into sequential phases, register them with an explicit
// dependency chain (phase N+1 depends_on phase N) and gate each launch so
// phase N is MERGED TO MAIN — its [conductor:<id>] commit reachable from
// origin/main — BEFORE phase N+1 launches. Phase N is also auto-tagged on
// landing (autotag.yml, STRAND A), so the merge both unblocks the next
// phase and publishes. PR #130's dependency-aware base in implement.js
// (Locate path b) stays as the substrate SAFETY NET, not a replacement for
// merging in order.
const PHASE_MERGE_GATE_DOCTRINE =
  'PHASE-MERGE GATE (multi-phase epics): if this plan is more than one '
  + 'phase, register the phases as an ORDERED dependency chain — phase N+1 '
  + 'carries dependencies=[<phase N task id>] (sequential, in order) so it '
  + 'cannot launch until phase N is DONE. The merge gate: phase N must be '
  + 'MERGED TO MAIN (its [conductor:<id>] commit reachable from origin/main, '
  + 'auto-tagged by autotag.yml) BEFORE the next phase is launched. Do NOT '
  + 'kick phase N+1 off bare main while phase N is still un-merged.'

// ── Phase 5: Register + track via conductor (planning steps only) ──────
phase('Track')
const planJson = JSON.stringify(plan, null, 2)
const linkStep = SID
  ? `\n1b. IMMEDIATELY AFTER task_create, call task_link_session(task_id=<the new task id>, session_id="${SID}") to tie this driving session to the new task (explicit session_id — never the request_id default).`
  : ''
const advSid = SID ? `, session_id="${SID}"` : ''
const tracking = await agent(
  `You are registering this plan as a conductor-tracked task in PRISM, then walking it through the PLANNING portion of the SDLC state machine ONLY (stop before build).\n\nThe WORKFLOW_STEPS are: review_previous_notes → draft_story → story_gate → verify_plan → plan_gate → write_failing_tests → red_gate → implement_tasks → verify_green_state → green_gate. Planning = the steps up to (and stopping at) verify_plan.\n\n${PHASE_MERGE_GATE_DOCTRINE}\n\nLoad tools: ToolSearch("select:mcp__prism__task_create,mcp__prism__task_link_session,mcp__prism__conductor_advance,mcp__prism__conductor_gate").\n\nSteps:\n1. task_create with title=plan.title VERBATIM — a SHORT human-friendly feature title (~4-9 words, plain language: what the feature IS, as a user would name it). Do NOT pack mechanics, phase/step lists, arrow-diagrams, or file/data-hook names into the title — ALL of that goes in the description. Use the plan summary as description, assigned_agent="sm", tags=["phase-router","prototype","planning"], AND persist the rich plan by passing plan_diagram=<the plan.plan_diagram Mermaid source> and plan_doc=<the plan.plan_doc markdown> so PRISM renders the plan as a document (diagram on top, proposed change below). If task_create does not carry them, call task_update(id, plan_diagram=..., plan_doc=...) immediately after. Capture the returned task id.${linkStep}\n2. conductor_advance(id${advSid}) to move from review_previous_notes -> draft_story, then conductor_advance(id${advSid}) again to land on story_gate (gate_state=pending).\n3. story_gate is RUBRIC-VERIFIED (story_complete: the YAML rubric scores the task's plan_doc — sections present, every AC has an id + oracle). Call conductor_gate(id, action="approve", reason="story rubric evidence: sections + AC ids + oracles present") WITHOUT override. If it returns ok:false, the rubric reason names exactly what the plan_doc is missing — FIX plan_doc via task_update and re-approve; do NOT reach for override=true (the forced-override path is retired).\n4. conductor_advance(id${advSid}) to reach verify_plan. STOP there — verify_plan is the planning/build boundary; do NOT enter plan_gate or write_failing_tests.\n\nReturn the task id, the current_step you ended on, the ordered list of steps you walked, and notes on anything that errored.\n\nPLAN:\n${planJson}`,
  { label: 'track:conductor', phase: 'Track', schema: TRACK_SCHEMA })

return { feature, coverage, sourceGapsChased: sourceFindings.length, plan, tracking }
