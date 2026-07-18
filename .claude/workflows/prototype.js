export const meta = {
  name: 'prototype',
  description: 'PRISM-first research -> source fallback -> synthesize plan -> build a clickable MOCK-data prototype (PRISM-skinned, isolated) -> register as a conductor-tracked planning task. The engine behind the /prototype phase router.',
  whenToUse: 'Run when starting a feature/phase router: mines PRISM knowledge first, fills gaps from source code, synthesizes a PRD-style plan, generates a clickable mock-data prototype you can SEE the feature in (no real data touched), and parks the work in conductor\'s planning steps.',
  phases: [
    { title: 'Recall', detail: 'Mine PRISM brain + memory in parallel' },
    { title: 'Coverage', detail: 'Judge what PRISM answered; list gaps' },
    { title: 'Source dive', detail: 'One agent per gap greps/reads real source' },
    { title: 'Plan', detail: 'Synthesize PRD-style plan from all evidence' },
    { title: 'Mock', detail: 'Generate a clickable MOCK-data prototype, PRISM-skinned + isolated' },
    { title: 'Track', detail: 'Create task + park at draft_story (no gate self-drive)' },
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
// The DRIVING Claude session id - passed via args by the orchestrator
// (workflow JS has no env/process access). GUARDED for the bare-string args
// case: only read session_id when _input is an object. Empty SID => today's
// behavior (never emit task_link_session / session_id with an empty value).
const SID = (_input && typeof _input === 'object' ? (_input.session_id || '') : '').trim()

// The conductor daemon the pre-flight probes. Defaults to the CANONICAL
// release web port (7778, what `prism start` binds and every customer runs);
// a dev instance on another port passes api_base (e.g. http://127.0.0.1:8888).
// Never hardcode a dev port — this script ships.
const API_BASE = ((_input && typeof _input === 'object' ? _input.api_base : '') || 'http://127.0.0.1:7778').replace(/\/$/, '')

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
    // the feature is - what a user would recognize it as. ~4-9 words, plain
    // language. NO mechanics, NO phase/step enumerations, NO data-hook/file
    // names, NO arrow-diagrams, NO "(initializing -> ... -> gates)" jargon.
    // ALL of that belongs in `summary`/description, never the title.
    // Good: "Animate the conductor task tile through its lifecycle".
    // Bad:  "Animate conductor task lifecycle (initializing -> live turns ->
    //        gates) + sub-agent lanes in lock step".
    title: { type: 'string', description: 'SHORT human-friendly feature title (~4-9 words, plain language): what the feature IS, as a user would name it. No mechanics, phase lists, arrow-diagrams, file/data-hook names, or jargon - those go in summary.' },
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
    plan_diagram: { type: 'string', description: 'Mermaid source (sequenceDiagram / classDiagram / flowchart) for the plan diagram. Consult the mermaid-syntax skill for valid syntax + Hermes theming - the plan_coverage rubric requires it to parse.' },
    plan_doc: { type: 'string', description: 'Markdown proposed-change/story doc rendered below the diagram. MUST carry the rubric-required sections (## Summary, ## Requirements, ## Acceptance Criteria) with requirement ids: FR-<n>/NFR-<n> bullets under Requirements and AC-<n> bullets under Acceptance Criteria, each AC ending in "- oracle: <observable check>". The story_gate/plan_gate rubrics score these mechanically.' },
  },
}

const MOCK_SCHEMA = {
  type: 'object',
  required: ['path', 'views', 'notes'],
  properties: {
    path: { type: 'string', description: 'absolute path to the written prototype CSF story (src/prototypes/<slug>.stories.tsx), composed from PRISM\'s REAL components' },
    url: { type: 'string', description: 'workshop deep-link to open the prototype (run `npm run workshop` in web/, then this Ladle story URL)' },
    views: { type: 'array', items: { type: 'string' }, description: 'the clickable views/journeys implemented' },
    notes: { type: 'string', description: 'what is mocked + how the journey was verified' },
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

const PREFLIGHT_SCHEMA = {
  type: 'object',
  required: ['identity_ok', 'halt_reason'],
  properties: {
    identity_ok: { type: 'boolean', description: 'prism_status.prism_version (MCP) === /api/version (conductor HTTP) — false = MCP bound to a different/fork daemon than the conductor' },
    mcp_ver: { type: 'string' },
    http_ver: { type: 'string' },
    halt_reason: { type: 'string' },
  },
}

// -- Phase 0: Pre-flight — MCP-vs-conductor daemon identity guard --------
// A duplicate ~/.claude.json key can bind mcp__prism__* to a DIFFERENT
// daemon (a fork, or another port) than the conductor HTTP endpoint. This
// prototype registers a planning task via MCP at the end (Track) — a
// misroute would park it on the WRONG store. Fail fast before any mutation.
phase('Pre-flight')
const _pf = await agent(
  `${PRISM_HINT}\n\nPRE-FLIGHT daemon-identity guard (READ-ONLY; use 127.0.0.1, NEVER localhost). Call \`prism_status\` (MCP) and read its \`prism_version\` (mcp_ver); \`curl -s -m5 ${API_BASE}/api/version\` and read that \`version\` (http_ver). Set identity_ok = (mcp_ver === http_ver). If they differ, your MCP tools are bound to a DIFFERENT daemon than the conductor and this prototype would register its planning task on the WRONG store — set identity_ok=false and halt_reason='MCP daemon <mcp_ver> != conductor daemon <http_ver> — /mcp reconnect prism to the daemon serving ${API_BASE} or restart the session'. Also read \`prism_status.data_dir\` for the store identity. Return {identity_ok, mcp_ver, http_ver, halt_reason}.`,
  { label: 'pre-flight', phase: 'Pre-flight', schema: PREFLIGHT_SCHEMA })
if (!_pf.identity_ok) {
  throw new Error(`PRE-FLIGHT HALT - ${_pf.halt_reason || 'MCP daemon != conductor daemon (identity mismatch)'} [mcp_ver=${_pf.mcp_ver} http_ver=${_pf.http_ver}]`)
}
log(`Pre-flight OK - MCP and conductor agree on daemon identity (${_pf.http_ver}).`)

// -- Phase 1: Recall (parallel mine of brain + memory) ------------------
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

// -- Phase 2: Coverage judgement ----------------------------------------
phase('Coverage')
const coverage = await agent(
  `Given this PRISM recall result for the feature "${feature}":\n\n${recallBlob}\n\nDecide what is genuinely ANSWERED vs what is still a GAP that requires reading actual source code. Be strict: a vague mention is not coverage. List each gap as a specific question. Set need_source_dive=true if any gap requires source inspection.`,
  { label: 'coverage:judge', phase: 'Coverage', schema: COVERAGE_SCHEMA })

// -- Phase 3: Source dive - one agent per gap, only if needed ------------
let sourceFindings = []
if (coverage.need_source_dive && coverage.gaps.length) {
  phase('Source dive')
  log(`PRISM left ${coverage.gaps.length} gap(s) - diving into source`)
  sourceFindings = (await parallel(coverage.gaps.slice(0, 6).map((gap) => () =>
    agent(
      `PRISM did not answer this gap for the feature "${feature}":\n\n"${gap}"\n\nResearch the REAL source under E:/.prism (use Grep, Glob, Read, and ToolSearch->mcp__prism__brain_call_chain if you need call flow). Cite concrete file:line. If the answer truly isn't in source either, say so explicitly.`,
      { label: `source:${gap.slice(0, 30)}`, phase: 'Source dive', schema: SOURCE_SCHEMA }))
  )).filter(Boolean)
} else {
  log('PRISM coverage sufficient - skipping source dive')
}

// -- Phase 4: Synthesize the plan / PRD ---------------------------------
phase('Plan')
const evidence = JSON.stringify({ recall: { brain, mem }, coverage, sourceFindings }, null, 2)
const plan = await agent(
  `Synthesize a PRD-style plan for the feature "${feature}" using ONLY the evidence below - do not invent capabilities the evidence doesn't support. Where evidence is thin, put it in open_questions rather than asserting it.\n\nAlso emit TWO rich-render fields:\n- plan_diagram: a valid Mermaid diagram (sequenceDiagram for actor<->system journeys, or classDiagram/flowchart for structure) capturing the core flow of this change. CONSULT THE mermaid-syntax SKILL (.claude/skills/mermaid-syntax) for valid syntax, the 6 supported diagram types, c4 layout science, and Hermes theming - the conductor's plan_coverage rubric mechanically requires plan_diagram to be present AND parse. Raw Mermaid source only - no code fences.\n- plan_doc: a markdown PROPOSED-CHANGE/STORY doc with the rubric-required sections - ## Summary, ## Requirements (FR-<n>/NFR-<n> bullet ids), ## Acceptance Criteria (AC-<n> bullet ids, each ending "- oracle: <observable check>") - plus plan steps and open questions. No raw JSON, no <pre>. The story_gate rubric scores these sections mechanically.\n\nEVIDENCE:\n${evidence}`,
  { label: 'plan:synthesize', phase: 'Plan', schema: PLAN_SCHEMA })

// -- Phase 4b: Mock prototype (clickable, MOCK data, PRISM-skinned) ------
// A plan is not demonstrable. The owner wants to SEE and CLICK the feature on
// MOCK data BEFORE any real code is touched (and without risking real data).
// Generate ONE self-contained HTML prototype, reskinned to the REAL PRISM
// tokens so it reads native, and attach its path to the plan.
phase('Mock')
const mock = await agent(
  `Build a clickable MOCK-DATA prototype of this plan's core journey as a **Ladle story composed from PRISM's REAL components** — NOT hand-rolled HTML. PRISM has a component workshop (Ladle: services/prism-service/prism_service/web, stories at src/**/*.stories.tsx) that renders the real @nous-research/ui + Hermes-themed components in isolation. A prototype is now a STORY in that workshop, so what the owner clicks IS the shipping UI with fake data (design review becomes trustworthy, not indicative). HARD RULES:\n`
  + `- Write ONE CSF story file to services/prism-service/prism_service/web/src/prototypes/<kebab-feature>.stories.tsx (this dir is gitignored — mock/scratch). Default-export { title: "Prototypes / <feature>" }; export the journey component as a named story.\n`
  + `- COMPOSE FROM REAL COMPONENTS — this is the whole point: import the actual components the app uses (from @/components/* and @nous-research/ui — e.g. Card, Lozenge, EntityChip). READ the existing src/components/*.stories.tsx and the Conductor/Plan/Task pages (StepRail/PlanView/TaskDetailPage/TasksPage) first to see what components + props exist. Do NOT hand-roll markup or copy CSS custom properties — the theme (Tailwind v4 + Hermes) comes from the workshop's .ladle/components.tsx automatically, so the story renders in the REAL theme with zero token-copying.\n`
  + `- ZERO backend / ZERO real data: bake mock JSON inline in the story. Nothing touches the daemon or real PRISM stores. This is so it CANNOT break real data.\n`
  + `- Multi-view click-through: a single React component with useState-driven view switching across the plan's primary views, each rendered from the real components + mock data.\n`
  + `- Visible MOCK banner (use a real component, e.g. a Lozenge or a Card) stating it is a MOCK-DATA prototype that touches nothing real.\n`
  + `- RETURN: the story file absolute path; the workshop deep-link \`url\` to open it (run \`npm run workshop\` in web/, then the story is a Ladle URL like http://localhost:61000/?story=prototypes--<slug>--<story-name> — fetch the Ladle sidebar to confirm the exact story id, the slugs are non-obvious); the views implemented; and how you verified the journey (the story renders in the workshop with the real theme).\n\nPLAN:\n`
  + JSON.stringify(plan, null, 2),
  { label: 'mock:prototype', phase: 'Mock', schema: MOCK_SCHEMA })
if (mock && mock.path) {
  plan.plan_doc = String(plan.plan_doc || '')
    + '\n\n## Mock prototype\n\nClickable MOCK-data prototype composed from PRISM\'s REAL components (Ladle workshop, no real data touched): '
    + mock.path + (mock.url ? '\nOpen: ' + mock.url : '') + '\nViews: ' + (mock.views || []).join(', ')
}

// -- Phase-merge gate doctrine (stacked-epic stranding fix, task 56458db1) --
// The implement workflow cuts EVERY task branch off origin/main, so a
// stacked multi-phase epic's phase N+1 depends on un-merged phase N
// substrate and hard-halts (memory project_implement_branches_off_main).
// The phase router MUST therefore enforce a PHASE-MERGE GATE: when this
// plan decomposes into sequential phases, register them with an explicit
// dependency chain (phase N+1 depends_on phase N) and gate each launch so
// phase N is MERGED TO MAIN - its [conductor:<id>] commit reachable from
// origin/main - BEFORE phase N+1 launches. Phase N is also auto-tagged on
// landing (autotag.yml, STRAND A), so the merge both unblocks the next
// phase and publishes. PR #130's dependency-aware base in implement.js
// (Locate path b) stays as the substrate SAFETY NET, not a replacement for
// merging in order.
const PHASE_MERGE_GATE_DOCTRINE =
  'PHASE-MERGE GATE (multi-phase epics): if this plan is more than one '
  + 'phase, register the phases as an ORDERED dependency chain - phase N+1 '
  + 'carries dependencies=[<phase N task id>] (sequential, in order) so it '
  + 'cannot launch until phase N is DONE. The merge gate: phase N must be '
  + 'MERGED TO MAIN (its [conductor:<id>] commit reachable from origin/main, '
  + 'auto-tagged by autotag.yml) BEFORE the next phase is launched. Do NOT '
  + 'kick phase N+1 off bare main while phase N is still un-merged.'

// -- Safe PARALLEL fan-out doctrine (distinct from the sequential phase chain) --
// The phase-merge gate above serializes DEPENDENT phases. This one is the
// opposite axis: when a plan splits into genuinely INDEPENDENT demonstrable
// features, set them up so the build phase can run them CONCURRENTLY without
// collisions. The two primitives that make wide fan-out SAFE rather than a
// merge-conflict pileup are: (1) DISJOINT allowed_files per child - the hard
// collision boundary; (2) a per-child proof_type/oracle so each slice clears
// its OWN gate shape (test/metric/artifact/demo) instead of override-everything
// (the v6.7.6 proof-type-driven gates). Children are reached via the ROOT epic;
// the epic stays the WATCHED conductor task and rolls children up at green_gate.
const FANOUT_DECOMPOSITION_DOCTRINE =
  'SAFE PARALLEL FAN-OUT (independent features - the concurrency axis): a '
  + 'SINGLE demonstrable feature stays ONE task; do NOT over-decompose into '
  + 'narrated phases. But when the plan genuinely splits into MULTIPLE '
  + 'independent demonstrable features, set the build phase up to fan them out '
  + 'CONCURRENTLY and SAFELY: register the epic as a ROOT task (parent_id="") '
  + 'and each feature as a CHILD (parent_id=<epic id>) carrying - (a) DISJOINT '
  + 'allowed_files: the collision boundary that makes parallel dev safe, no two '
  + 'children may share a file (if two slices must touch the same file they are '
  + 'NOT independent - keep them one task or sequence them); (b) its OWN '
  + 'proof_type + oracle matched to how THAT feature is proven '
  + '(test|metric|artifact|demo) so each child clears its own gate shape WITHOUT '
  + 'override; (c) stop_if guards. Truly independent children carry NO '
  + 'dependencies (parallel); only a real substrate dependency gets a depends_on '
  + '(then the PHASE-MERGE GATE applies). The ROOT epic is what is WATCHED on '
  + '/conductor and rolls child proofs up at its green_gate - keep it root '
  + '(parent_id=""). Express this split as a "## Fan-out slices" section in the '
  + 'epic plan_doc (slice -> files -> proof_type) even when you register the '
  + 'children, so the decomposition is visible, not implicit.'

// -- Phase 5: Register + track via conductor (planning steps only) ------
phase('Track')
const planJson = JSON.stringify(plan, null, 2)
const linkStep = SID
  ? `\n1b. IMMEDIATELY AFTER task_create, call task_link_session(task_id=<the new task id>, session_id="${SID}") to tie this driving session to the new task (explicit session_id - never the request_id default).`
  : ''
const advSid = SID ? `, session_id="${SID}"` : ''
const tracking = await agent(
  `You are registering this plan as a conductor-tracked task in PRISM and parking it at a WORKABLE authoring step. You DO NOT walk it through any GATE. A prototype planning task is single-author (this session produced the story), so the story_gate distinct-actor guard would refuse a self-approval - never drive INTO a gate you cannot clear, and NEVER use override=true (the forced-override path is retired). The card must land as clean open work (draft_story), not a red failed gate.\n\nThe WORKFLOW_STEPS are: review_previous_notes -> draft_story -> story_gate -> verify_plan -> plan_gate -> write_failing_tests -> red_gate -> implement_tasks -> verify_green_state -> green_gate. You will advance ONLY as far as draft_story and STOP - a whole session-turn short of story_gate.\n\n${PHASE_MERGE_GATE_DOCTRINE}\n\n${FANOUT_DECOMPOSITION_DOCTRINE}\n\nLoad tools: ToolSearch("select:mcp__prism__task_create,mcp__prism__task_link_session,mcp__prism__conductor_advance").\n\nSteps:\n1. task_create with title=plan.title VERBATIM - a SHORT human-friendly feature title (~4-9 words, plain language: what the feature IS, as a user would name it). Do NOT pack mechanics, phase/step lists, arrow-diagrams, or file/data-hook names into the title - ALL of that goes in the description. Use the plan summary as description, assigned_agent="sm", tags=["phase-router","prototype","planning"], AND persist the rich plan by passing plan_diagram=<the plan.plan_diagram Mermaid source> and plan_doc=<the plan.plan_doc markdown> so PRISM renders the plan as a document (diagram on top, proposed change below). If task_create does not carry them, call task_update(id, plan_diagram=..., plan_doc=...) immediately after. Capture the returned task id.\n1a. SET THE PROOF SHAPE: pass proof_type + oracle on task_create (or task_update right after) matched to HOW this feature is proven - proof_type="test" is the TDD default; use "metric"/"artifact"/"demo" only when that is the REAL oracle (e.g. an analyzer-count, a generated file, a UI capture). This makes the build phase's red/green gates validate the right shape instead of demanding a failing test. If the plan splits into INDEPENDENT demonstrable features, apply the SAFE PARALLEL FAN-OUT doctrine above: register the epic as the ROOT task and each feature as a CHILD (parent_id=<epic id>) with DISJOINT allowed_files + its own proof_type/oracle, so the build phase can drive them concurrently without collisions.${linkStep}\n2. conductor_advance(id${advSid}, fields=["from_step","to_step","gate_state","rubric"]) to move from review_previous_notes -> draft_story, then conductor_advance(id${advSid}, fields=["from_step","to_step","gate_state","rubric"]) again to LAND ON draft_story. The fields projection keeps the response lean (no echoed task object); the advance INTO draft_story returns result['rubric'] (the story_complete rubric) - confirm plan_doc already matches it (sections + AC-<n> ids + "oracle:" markers) so whoever works this card next clears story_gate first try.\n3. STOP at draft_story. Do NOT advance into story_gate. Do NOT call conductor_gate at all - no approve, no override. The card is now a workable planning ticket someone (a distinct actor) can pick up and drive through the gate when the build is scheduled.\n\nReturn the task id, the current_step you ended on (draft_story), the ordered list of steps you walked, and notes on anything that errored.\n\nPLAN:\n${planJson}`,
  { label: 'track:conductor', phase: 'Track', schema: TRACK_SCHEMA })

return { feature, coverage, sourceGapsChased: sourceFindings.length, plan, mock, tracking }
