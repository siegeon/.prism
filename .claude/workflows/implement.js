export const meta = {
  name: 'implement',
  description: 'Drive one PRISM task through the conductor-gated SDLC (review -> story -> story_gate -> verify_plan -> plan_gate -> red tests -> red_gate -> implement -> verify_green -> green_gate). story_gate/plan_gate are rubric-verified (no override on a compliant drive). Brain is the primary knowledge source; grepping source on disk is the fallback. The build-half companion to the `prototype` planning workflow.',
  whenToUse: 'Run to actually WORK a task through PRISM\'s conductor. Invoke as Workflow({name:"implement", args:{task_id:"<uuid>"}}). Omit task_id to let it pull task_next. Pass {dry_run:true} to trace the whole SDLC read-only (no conductor mutations, no file writes), or {stop_after:"red_gate"} to halt the drive at a named step.',
  phases: [
    { title: 'Locate', detail: 'Read task + conductor state; brain-first context; ensure a feature branch' },
    { title: 'Review notes', detail: 'sm: prior notes/decisions/memory' },
    { title: 'Draft story', detail: 'sm: story + acceptance criteria from the task' },
    { title: 'Verify plan', detail: 'sm: confirm the plan covers every requirement' },
    { title: 'Red tests', detail: 'qa: write failing tests that pin the requirements' },
    { title: 'Red gate', detail: 'gate: verifier confirms tests are RED' },
    { title: 'Implement', detail: 'dev: smallest change to turn the suite green' },
    { title: 'Verify green', detail: 'qa: full suite + acceptance verification' },
    { title: 'Green gate', detail: 'gate: terminal sign-off; mark task done' },
  ],
}

// -- Input normalization ------------------------------------------------
// args may arrive as a JSON string, a plain object, or a bare task id.
let _in = args
if (typeof _in === 'string') {
  try { _in = JSON.parse(_in) } catch { _in = { task_id: _in } }
}
_in = _in && typeof _in === 'object' ? _in : {}
const TASK_ID = (_in.task_id || _in.id || '').trim()
const DRY = _in.dry_run === true || _in.dry_run === 'true'
const STOP_AFTER = (_in.stop_after || '').trim() // e.g. 'red_gate' to halt the drive there
// The DRIVING Claude session id - sourced by the orchestrator from
// CLAUDE_CODE_SESSION_ID and passed in via args (workflow JS has no
// env/process access). Threaded into task_link_session + conductor_advance
// so the task<->session JOIN resolves. GUARDED: empty SID => today's behavior.
const SID = (_in.session_id || '').trim()
// Telemetry run id for the agent-run spine (task f4498190). One drive of this
// workflow == one run_id; every step's agent() emits a row under it. Sourced
// from args when the orchestrator supplies one, else derived from SID/task.
const RUN_ID = (_in.run_id || _in.runId || SID || TASK_ID || 'run-adhoc').trim()
// Where the agent-run telemetry POSTs land. Defaults to the dev web port
// (8888 on this box); overridable via args.api_base for the WSL release
// topology (7778). The MCP daemon serves /api/* on the same FastAPI app.
const API_BASE = (_in.api_base || 'http://127.0.0.1:8888').replace(/\/$/, '')

// -- Conductor state machine (mirror of models/workflow.py:WORKFLOW_STEPS) -
// Four blocking gates. story_gate/plan_gate are RUBRIC-VERIFIED (task
// 8579d49e): pure YAML-rubric functions score the task's plan_doc/
// plan_diagram server-side (services/arc_governance.py) - a compliant
// story/plan clears them WITHOUT override; a rubric rejection names the
// missing section/AC/oracle/principle to fix. red_gate/green_gate stay
// shell-verifier gates.
//
// VERIFIER-BLINDNESS NOTE: the conductor's gate verifier (VerifierService)
// scopes Tier-0 git to the MCP daemon's cwd, NOT this working checkout
// (project_context wires workspace=None; conductor._verify_gate passes no
// workspace). In a source-run dev topology the daemon often cannot see
// working-tree edits, so a gate's verifier returns status=error / "no diff
// in scope" even though the change is real. Gate handlers below treat that
// specific signal as STRUCTURALLY BLIND and recover via override using the
// agent's OWN executed test trace as evidence - but never override a gate
// whose verifier actually saw the diff and reported a genuine failure.
const ORDER = [
  'review_previous_notes', 'draft_story', 'story_gate',
  'verify_plan', 'plan_gate',
  'write_failing_tests', 'red_gate',
  'implement_tasks', 'verify_green_state', 'green_gate',
]

// -- Shared agent preamble ----------------------------------------------
const PRISM_TOOLS = 'You have PRISM MCP tools via ToolSearch. Load what you need, e.g. ToolSearch("select:mcp__prism__brain_search,mcp__prism__brain_understand,mcp__prism__brain_call_chain,mcp__prism__memory_recall,mcp__prism__task_list,mcp__prism__conductor_advance,mcp__prism__conductor_gate,mcp__prism__task_update"). Project slug is "prism".'

const KNOWLEDGE = [
  'KNOWLEDGE PROTOCOL - Brain is the primary repository, disk is the fallback:',
  '1. FIRST query the Brain: brain_search (try 3-4 query variants), brain_understand for a subgraph, brain_call_chain for blast radius, memory_recall for conventions/decisions.',
  '2. ONLY for what the Brain does not answer, fall back to Grep/Glob/Read on source under E:/.prism.',
  '3. Read before you cite. Every claim about code carries a concrete file:line. Never cite an unread source.',
].join('\n')

// PROCEDURAL SPINE - the small STATIC set of hard procedural rules that must
// ALWAYS hold regardless of what lives in memory (task 0c811636). These are the
// invariants the workflow itself depends on (branch safety, write chunking,
// hook semantics, board hygiene); they are NOT memories and must never drift
// out from under the drive. The LIVING feedback conventions (the domain=
// "feedback" doctrine: render-policy, gate-enforcement, board-hygiene, ...) are
// pushed in on TOP of this spine from context_bundle["conventions"] - see
// preamble() / setLiveConventions(). They are deliberately NOT spelled out here
// so the frozen list cannot drift the moment a memory is added.
const PROCEDURAL_SPINE = [
  'PRISM PROCEDURAL SPINE (hard rules - always apply):',
  '- Never commit to main/master/staging/develop. Work on the feature branch this workflow created.',
  '- File writes: max ~30 lines per edit operation; chunk larger writes.',
  '- Hooks are advisory (exit 0) - never block tool execution.',
  '- Destructive ops: validate paths, never -ErrorAction SilentlyContinue, no inline destructive PowerShell.',
  '- If the change is user-visible, patch-bump PRISM_VERSION in the same commit.',
  '- TASK-BOARD HYGIENE (hard): NEVER call task_create for a ROOT/parent task. You are driving ONE task through its SDLC - its internal breakdown is narration, NOT new board tasks. The board shows only the top-level task; sub-work is tracked by commits and the conductor steps, not by spawning siblings.',
  '- The ONLY task_create you may issue is a DISPOSABLE verification fixture (e.g. a throwaway task to put a conductor gate into a pending state so you can exercise the gate UI). It MUST set parent_id="<the task id this workflow is driving>" so it stays OFF the root board, MUST be tagged "ephemeral-fixture", and MUST be set status="cancelled" the moment the verification screenshot/assertion is captured. Prefer reusing the driven task itself over creating any fixture.',
].join('\n')

// LIVE CONVENTIONS - push-injected (arc-kit model) from context_bundle's
// importance-ranked, top-N-capped domain="feedback" conventions. Empty until
// the Locate step calls context_bundle and seeds it via setLiveConventions();
// once seeded, EVERY subsequent step agent's preamble carries the living
// conventions deterministically (push, not the old unreliable pull). The drive
// no longer hand-maintains a frozen CONVENTIONS array that silently drifts the
// moment a memory is added.
let LIVE_DOCTRINE = ''

// Render the bundle's capped conventions into a compact preamble block. Accepts
// the array of {name, importance, description|summary, ...} the bundle exposes
// under bundle["conventions"]; tolerates dicts or already-formatted strings.
function setLiveConventions(conventions) {
  if (!Array.isArray(conventions) || conventions.length === 0) return
  const lines = conventions.map((c) => {
    if (typeof c === 'string') return `- ${c}`
    const name = c.name || c.id || 'convention'
    const imp = (c.importance != null) ? ` [importance ${c.importance}]` : ''
    const body = (c.summary || c.description || '').toString().replace(/\s+/g, ' ').trim()
    return `- ${name}${imp}: ${body}`
  })
  LIVE_DOCTRINE = [
    'LIVE PRISM CONVENTIONS (push-injected from context_bundle - importance-ranked, top-N capped; honor these as project doctrine):',
    ...lines,
  ].join('\n')
}

// SELF-HEAL doctrine (implement-workflow reliability). Injected into every step
// agent's preamble so a failed step / genuine gate-reject / runtime error
// triggers a knowledge-ladder climb instead of a dead halt. NOTE: the RESEARCH
// rung is WebSearch/WebFetch today; Context7 MCP (clean library docs) is a
// not-yet-wired follow-up.
const SELF_HEAL = [
  'SELF-HEAL DOCTRINE - when a step fails, a gate GENUINELY rejects (NOT the verifier-blind case above), or a tool/runtime error fires, climb the ladder before giving up:',
  '1. PRISM-FIRST: brain_search + memory_recall for a known resolution. PRISM is the knowledge AND time authority - it server-stamps run timestamps; never reach for a client clock.',
  '2. RESEARCH: if PRISM has no answer, research best practices via WebSearch/WebFetch and cite sources.',
  '3. APPLY the smallest fix.',
  '4. RECORD: memory_store(type="failure", with file:line + the fix) so PRISM HAS THE ANSWER next time - failures become memories.',
].join('\n')

const dryNote = DRY
  ? '\n\nDRY-RUN MODE: Do NOT write files, do NOT run conductor_advance/conductor_gate/task_update, do NOT mutate anything. Only gather context and report exactly what you WOULD do. Treat the conductor transition as simulated and report the to_step you would expect.'
  : ''

function preamble(role) {
  // Static procedural spine ALWAYS; the live push-injected conventions ride on
  // top once the Locate step has seeded them from context_bundle. SELF_HEAL's
  // memory_recall stays the FALLBACK path, not the primary convention source.
  const conventionsBlock = LIVE_DOCTRINE
    ? `${PROCEDURAL_SPINE}\n\n${LIVE_DOCTRINE}`
    : PROCEDURAL_SPINE
  return `${PRISM_TOOLS}\n\nYou are acting as the PRISM "${role}" persona inside the conductor SDLC.\n\n${KNOWLEDGE}\n\n${conventionsBlock}\n\n${SELF_HEAL}${dryNote}`
}

// -- Step-contract telemetry transport (task 0b34b6f7) -------------------
// The Workflow script sandbox has NO fetch / Node net API - an in-sandbox
// HTTP call throws 'fetch is not defined' on every drive and ZERO rows
// land. The channel that DOES exist is the STEP AGENT's own Bash: every
// step's contract instructs a curl POST of its agent_runs row to the
// ingest API. postAgentRun (below) stays the single row-shape authority
// and journals the canonical row into the drive log.
function telemetryInstr(stepId) {
  if (DRY) return ''
  const role = ROLE_BY_STEP[stepId] || 'agent'
  const tid = (typeof locate !== 'undefined' && locate.task_id) || TASK_ID
  return `\n\nTELEMETRY (step contract - REQUIRED LAST ACTION): POST your agent-run row with Bash curl (use 127.0.0.1, NEVER localhost): \`curl -s -m5 -X POST '${API_BASE}/api/agent-runs/ingest?project=prism' -H 'Content-Type: application/json' -d '<ROW>'\` where <ROW> is the JSON object {"run_id":"${RUN_ID}","workflow_name":"implement","task_id":"${tid}","session_id":"${SID}","agent_id":"${RUN_ID}:${stepId}","role":"${role}","step":"${stepId}","ok":<your step ok, true|false>,"gate_state":"<final gate_state, or null>","verdict_summary":"<one-line validation/evidence summary>"}. The response must be {"ok":true,...} - quote that receipt in evidence. A failed POST is non-fatal: note it and continue (timing is stamped server-side on ingest).`
}

// -- Agent-run telemetry emitter (task f4498190) -------------------------
// ONE shared row builder + POST so the serial loop AND the parallel fanout
// wrapper emit an IDENTICAL row shape - no telemetry gap between the two
// execution paths. The agent_runs spine is idempotent on
// (run_id, agent_id, step), so a retried step UPDATES rather than dupes.
// Per-agent agentId/timing/tokens/tool_uses live in the harness journal +
// completion notification; we thread whatever the step result + meta expose
// (duration_ms/tokens/tool_uses), and ALWAYS thread the plumbed SID as
// session_id. Guarded end-to-end: a failed POST never breaks the drive.
async function postAgentRun(res, meta) {
  if (DRY) return // dry-run never mutates / never POSTs telemetry
  meta = meta || {}
  const row = {
    run_id: RUN_ID,
    workflow_name: 'implement',
    task_id: (res && res.task_id) || (typeof locate !== 'undefined' && locate.task_id) || TASK_ID,
    session_id: SID,
    agent_id: meta.agent_id || `${RUN_ID}:${meta.step || (res && res.step) || meta.label}`,
    parent_agent_id: meta.parent_agent_id || null,
    role: meta.role || null,
    step: meta.step || (res && res.step) || meta.label || null,
    model: meta.model || null,
    started_at: meta.started_at || null,
    ended_at: meta.ended_at || null,
    duration_ms: meta.duration_ms != null ? meta.duration_ms : null,
    tokens: meta.tokens != null ? meta.tokens : null,
    tool_uses: meta.tool_uses != null ? meta.tool_uses : null,
    ok: res ? res.ok === true : null,
    gate_state: (res && res.gate_state) || meta.gate_state || null,
    verdict_summary: (res && (res.validation || res.evidence)) || meta.verdict_summary || null,
    evidence_ref: (res && res.evidence) ? String(res.evidence).slice(0, 200) : null,
  }
  // SANDBOX-REAL TRANSPORT (task 0b34b6f7): the Workflow sandbox cannot do
  // HTTP itself (no fetch / Node net API), so the POST to
  // /api/agent-runs/ingest travels via the STEP AGENT's Bash curl - see
  // telemetryInstr, threaded into every step's contract. Here we journal
  // the canonical row so serial AND fanout paths share ONE shape and the
  // drive log carries the telemetry record alongside the agent's receipt.
  log(`agent-run row (POSTed by the step agent via Bash curl): ${JSON.stringify(row)}`)
  return row
}

// Parallel fanout wrapper: run several step handlers concurrently and emit the
// SAME telemetry row shape per branch via the shared postAgentRun emitter, so
// a future parallel drive has NO telemetry gap vs the serial loop. Unused by
// the default serial drive but kept on the same emitter contract.
async function fanout(jobs) {
  return Promise.all(jobs.map(async (j) => {
    const res = await j.run()
    // Timing is stamped server-side on ingest (workflow scripts forbid client clocks).
    await postAgentRun(res, { role: j.role, step: j.step, model: j.model })
    return res
  }))
}

// -- Schemas -------------------------------------------------------------
const LOCATE_SCHEMA = {
  type: 'object',
  required: ['task_id', 'title', 'current_step', 'gate_state', 'branch', 'context_summary', 'requirements'],
  properties: {
    task_id: { type: 'string' },
    title: { type: 'string' },
    current_step: { type: 'string', description: 'workflow_step from task_list; "" means not yet entered (next is review_previous_notes)' },
    gate_state: { type: 'string', enum: ['none', 'pending', 'passed', 'failed'] },
    branch: { type: 'string', description: 'git branch the build will land on (never main)' },
    context_summary: { type: 'string', description: 'brain-first summary of the relevant subsystem with file:line refs' },
    requirements: { type: 'array', items: { type: 'string' }, description: 'discrete acceptance requirements distilled from the task' },
    conventions: {
      type: 'array',
      description: 'the importance-ranked, top-N-capped conventions from context_bundle(persona="dev")["conventions"] - push-injected into every step agent\'s preamble (task 0c811636). Each item: {name, importance, summary|description}. Empty array if context_bundle returned none.',
      items: { type: 'object' },
    },
  },
}

const STEP_SCHEMA = {
  type: 'object',
  required: ['step', 'ok', 'to_step', 'gate_state', 'evidence'],
  properties: {
    step: { type: 'string' },
    ok: { type: 'boolean', description: 'true if the step work succeeded AND the conductor transition was accepted' },
    to_step: { type: 'string', description: 'the workflow_step the task now sits on (from the advance/gate result)' },
    gate_state: { type: 'string' },
    validation: { type: 'string', description: 'the validation note/evidence passed to conductor' },
    evidence: { type: 'string', description: 'what was done, with file:line and the exact verification command + result' },
    // AC7 - cite-your-source tier contract: every step DECLARES which knowledge
    // tier actually answered it (brain=why / grep=how / web=new-practice). A
    // step that cites NOTHING, or the WRONG tier (e.g. grepping for the WHY, or
    // claiming brain when only disk grep was consulted), is a mis-tiered cite
    // and is REJECTED - set ok:false with the mis-tier in halt_reason rather
    // than passing a tier-less / mis-tiered step.
    source_tier: { type: 'string', enum: ['brain', 'grep', 'web'], description: 'which tier answered this step: brain=WHY (decisions/rationale via brain_search/memory_recall), grep=HOW (concrete code mechanics via disk Grep/Read), web=NEW-PRACTICE (an unground-able approach validated by a cited WebSearch). A step citing nothing or the WRONG tier (e.g. grep for the WHY) is a mis-tiered cite and must be REJECTED.' },
    halt_reason: { type: 'string', description: 'set ONLY if the step failed, a gate rejected, OR the cite is empty/mis-tiered; empty otherwise' },
  },
}

// -- Phase: Pre-flight (fail fast) ---------------------------------------
// AC1: assert the run can even succeed BEFORE the drive - turns 7-min-to-fail
// runs into ~5-second fails with one actionable line. Workflow scripts have no
// fs/process access, so the checks run inside the first agent (read-only bash).
const PREFLIGHT_SCHEMA = {
  type: 'object',
  required: ['ok', 'sane_branch', 'datenow_clean', 'daemon_ok', 'deps_present', 'halt_reason'],
  properties: {
    ok: { type: 'boolean' },
    sane_branch: { type: 'boolean' },
    datenow_clean: { type: 'boolean' },
    daemon_ok: { type: 'boolean' },
    deps_present: { type: 'boolean', description: 'every DONE depends_on task\'s [conductor:<dep8>] commit is reachable from the chosen base OR carried by some branch (git log --all)' },
    halt_reason: { type: 'string', description: 'ONE actionable remediation line if ok=false; empty otherwise' },
  },
}
phase('Pre-flight')
const preflight = await agent(
  `${PRISM_TOOLS}\n\nPRE-FLIGHT GUARD - run these READ-ONLY checks from E:/.prism and fail FAST. Use 127.0.0.1, NEVER localhost (gitbash resolves localhost->::1 while the daemon binds IPv4 - a silent-zero trap that has burned whole runs).\n\n1. SANE BRANCH: after \`git -C E:/.prism fetch -q origin main\`, read \`git -C E:/.prism rev-parse --abbrev-ref HEAD\` and \`git -C E:/.prism rev-list --count HEAD..origin/main\`. sane_branch=false if the current branch is BEHIND origin/main by >0 (the stale-branch trap - the drive would build on stale code). main or an even/ahead feature branch is fine.\n2. CLOCK-CLEAN: \`git -C E:/.prism grep -nE 'Date[.]now|new[[:space:]]*Date[(]' -- .claude/workflows/*.js\`. datenow_clean=false on ANY match - PRISM is the time authority (it server-stamps run timestamps); a workflow script must never use a client clock (unavailable in the sandbox; breaks resume/cache; PRISM memory mx-9945f2).\n3. DAEMON/CONDUCTOR REACHABLE: \`curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8888/api/version\` must be 200 - the conductor cannot record transitions (or stamp time) against a dead daemon.${DRY ? ' (DRY-RUN: treat a dead daemon as non-fatal.)' : ''}\n4. DEPENDENCY PRESENCE: read this task's \`depends_on\` via task_list (the dependencies/depends_on field). For EACH depends_on dep that is DONE, confirm its substrate is present: its [conductor:<dep8>] commit (first 8 chars of the dep id) must be reachable from the chosen base OR carried by some branch - \`git -C E:/.prism log --all --grep "conductor:<dep8>" -n1\` must hit, and that commit must be an ancestor of origin/main OR of a branch returned by \`git -C E:/.prism branch -a --contains <sha>\`. deps_present=false ONLY when a DONE dep's commit is unreachable from the base AND no branch carries it. (No deps, or all dep commits reachable, => deps_present=true.)\n\nSet ok=true ONLY if sane_branch AND datenow_clean AND deps_present${DRY ? '' : ' AND daemon_ok'}. If ok=false, halt_reason = ONE actionable line, e.g. "branch <b> is N behind origin/main - rebase or branch fresh off main", "client clock in <file>:<line> - PRISM server-stamps time, remove it (mx-9945f2)", "daemon down on :8888 - start dev (prism-dev) before driving", or "dep <id> done but not merged to main and no branch carries it - merge it first".`,
  { label: 'pre-flight', phase: 'Pre-flight', schema: PREFLIGHT_SCHEMA })
if (!preflight.ok) {
  throw new Error(`PRE-FLIGHT HALT - ${preflight.halt_reason || 'a pre-flight check failed'} [sane_branch=${preflight.sane_branch} clock_clean=${preflight.datenow_clean} daemon_ok=${preflight.daemon_ok} deps_present=${preflight.deps_present}]`)
}
log(`Pre-flight OK - branch sane, workflow scripts clock-clean (PRISM owns time)${DRY ? '' : ', daemon reachable'}.`)

// -- Phase: Locate -------------------------------------------------------
phase('Locate')
const pick = TASK_ID
  ? `Read the driven task via task_list(id="${TASK_ID}") - a LEAN by-id read that returns JUST this one task (a 1-element list), NOT the whole board (a full board is ~100x the tokens).`
  : 'No task id was supplied. Call task_next to choose the highest-priority unblocked task; it returns that single task (do NOT pull the whole board).'

const locate = await agent(
  `${preamble('analyst')}\n\nGOAL: locate the task and orient before the conductor drive.\n\n${pick}\n\nThen:\n- Report current_step (workflow_step) and gate_state exactly as stored.\n- Build a brain-first context_summary of the subsystem this task touches (brain_search/brain_understand first, disk grep only for gaps), with file:line refs.\n- Distill the task description + any acceptance criteria into a discrete \`requirements\` list - each item independently testable.\n- PUSH-INJECT CONVENTIONS (task 0c811636): call \`context_bundle(persona="dev")\` ONCE and return its \`conventions\` array verbatim as the \`conventions\` field - this is PRISM's importance-ranked, top-N-capped living feedback doctrine (the domain="feedback" conventions - render policy, gate enforcement, board hygiene, etc.) that the drive injects into EVERY subsequent step agent's preamble. If the bundle has no \`conventions\` key or it is empty, return \`[]\` (the drive falls back to the static procedural spine + memory_recall self-heal).\n- BRANCH: (DEPENDENCY-AWARE BASE) first read this task's \`depends_on\` (the dependencies/depends_on field on the task row) BEFORE choosing the base, so the substrate the build needs is actually present. Resolve the base like so:\n  (a) NO deps, or every dep's [conductor:<dep8>] commit is already reachable from origin/main => base = origin/main (fresh feature branch off origin/main - unchanged from today).\n  (b) A dep that is DONE but whose [conductor:<dep8>] commit is NOT on origin/main yet EXISTS on some branch: find the containing branch via \`git -C E:/.prism log --all --grep "conductor:<dep8>" -n1 --format=%H\` then \`git -C E:/.prism branch -a --contains <sha>\` => base = that dep's containing branch, so the substrate is present and the run reaches red_gate instead of hard-halting at write_failing_tests (memory mx-a56419). NEVER pick a base that is BEHIND origin/main: verify with \`git -C E:/.prism rev-list --count <base>..origin/main\` (must be 0); if the only containing branch is behind origin/main, do NOT use it.\n  (c) A dep that is NOT done => the task is BLOCKED: do NOT drive - set the branch as the base you could not safely resolve and report it, but the dependency is not done so the workflow must not proceed (treat as blocked).\n  Then: if currently on main/master/staging/develop${DRY ? ', report the branch name (feat/<task-slug>) you WOULD create AND the dependency-derived base it WOULD be cut from (do not create it).' : ', create the feature branch (e.g. feat/<task-slug>) off the dependency-derived base resolved above and switch to it, then report its name and the base.'}\n${DRY ? '' : '- REQUIRED FIRST ACTION: immediately call task_update(id, status="in_progress") - do this before anything else so the tasks/kanban view shows the task as actively worked (not stranded in the pending column) while the SDLC runs.'}${SID && !DRY ? `\n- IMMEDIATELY AFTER that first action, call task_link_session(task_id="${TASK_ID}", session_id="${SID}") to tie this driving session to the task (explicit session_id - never the request_id default).` : ''}\n\nReturn the structured locate result.`,
  { label: 'locate', phase: 'Locate', schema: LOCATE_SCHEMA })

const startStep = locate.current_step && ORDER.includes(locate.current_step)
  ? locate.current_step
  : 'review_previous_notes'
const startIdx = ORDER.indexOf(startStep)
log(`Task "${locate.title}" is at "${startStep}" (gate=${locate.gate_state}) on branch ${locate.branch}. Driving${DRY ? ' (DRY-RUN)' : ''} from there.`)

// Seed the push-injected live conventions from context_bundle (task 0c811636).
// From here on EVERY step handler's preamble() carries the importance-ranked,
// top-N-capped feedback doctrine - replacing the old frozen CONVENTIONS array.
// memory_recall (SELF_HEAL) stays the fallback, not the primary source.
setLiveConventions(locate.conventions)
const _convCount = Array.isArray(locate.conventions) ? locate.conventions.length : 0
log(`Push-injected ${_convCount} live convention(s) from context_bundle into every step preamble.`)

// -- Per-step handler prompts --------------------------------------------
const ctx = `TASK: ${locate.title} (id ${locate.task_id})\nBRANCH: ${locate.branch}\nREQUIREMENTS:\n- ${locate.requirements.join('\n- ')}\n\nCONTEXT (brain-first):\n${locate.context_summary}`

function advanceInstr(stepId, validationHint) {
  // GUARDED: thread session_id=SID into the emitted conductor_advance call so
  // every non-gate step refreshes the task<->session link. When SID is empty
  // the arg is dropped entirely (byte-identical to today's no-session call).
  const sidArg = SID ? `, session_id="${SID}"` : ''
  // LEAN + RUBRIC-AWARE advance (v6.7.6/6.7.7). The fields projection returns
  // just the transition keys (+rubric on authoring steps) and DROPS the echoed
  // task object - the per-step verbosity tax is what caps how wide an epic can
  // fan out before the driver's context blows, so keep every advance lean.
  const intoAuthoring = stepId === 'review_previous_notes' || stepId === 'verify_plan'
  const rubricNote = intoAuthoring
    ? ` The to_step is an AUTHORING step, so this advance returns result['rubric'] (required sections, the AC-<n> id pattern, the "oracle:" marker) - shape the plan_doc/story to MATCH it NOW so the next gate passes first try (no re-plan round-trip).`
    : ''
  return `After the work, call conductor_advance(id="${locate.task_id}"${sidArg}, validation="${validationHint}", fields=["from_step","to_step","gate_state","rubric"]) to leave "${stepId}" - the fields projection keeps the drive lean by dropping the echoed task object. Report the returned to_step and gate_state.${rubricNote}\n` +
    `DESYNC TOLERANCE (do NOT false-halt): the conductor auto-advances a passing agent step to the NEXT GATE, so by the time this step's advance fires the task may ALREADY be past "${stepId}" or parked at a pending gate. If conductor_advance returns ok:false with a reason meaning the task is already at/past this step or a gate is PENDING (e.g. "gate '...' is pending; call gate_decide before advancing"), that is NOT a failure - this step's WORK is done and the conductor is merely ahead. In that case set ok:TRUE and report the task's ACTUAL current to_step + gate_state (from the advance result, or re-read via task_list(id="${locate.task_id}", fields=["workflow_step","gate_state"])) so the drive proceeds to the gate handler. Set ok:FALSE (reason in halt_reason) ONLY for a GENUINE rejection: the validation was rejected, the task was not found, or this step's own work failed.` +
    telemetryInstr(stepId)
}

// Commit the step's changes on the feature branch so (a) the eventual PR is
// real and (b) the gate verifier has a committed diff IF it can see this tree.
const commitInstr = (kind, scope) => DRY
  ? ''
  : ` Then COMMIT this step's changes on the feature branch (NEVER on main): \`git add <the files you touched> && git commit -m "${kind}(${scope}): <one-line> [conductor:${locate.task_id.slice(0, 8)}]"\`. Report the commit short-sha in evidence.`

// Gate recovery doctrine, shared by both blocking gates. The agent's OWN
// executed test command is the ground truth; the conductor verifier may be
// blind to this checkout (see VERIFIER-BLINDNESS NOTE).
// PROOF-TYPE-AWARE gate doctrine (v6.7.6). The gates validate against the
// task's DECLARED proof_type, so the agent must produce the proof that MATCHES
// it - NOT always a failing-test trace. This is what lets an epic fan out
// HETEROGENEOUS children (a test slice, a metric slice, an artifact slice, a
// ui slice) and have EACH clear its own gate honestly instead of override-
// everything. test stays the TDD default; the red/green shape generalizes to
// before/after for the non-test oracles.
const gateDoctrine = (expectWord, evidenceWord) =>
  `GATE DOCTRINE (proof_type-aware - the gate scores the task's OWN proof_type, so gather the matching proof, not always a test trace). Read the task's proof_type FIRST (task_list), then:\n` +
  `- proof_type=test (the TDD default): run the real test command yourself and capture the trace - it must be ${evidenceWord === 'red' ? 'RED (failing for the right reason)' : 'GREEN'}.\n` +
  `- proof_type=metric (incl. build-count): capture the number for THIS gate - ${evidenceWord === 'red' ? 'the BASELINE "before" count' : 'the improved "after" count'}; the count-delta IS the proof (record it in completion_proof, e.g. "warnings 12 -> 0"). No failing TEST need exist.\n` +
  `- proof_type=artifact: show the produced file/path (${evidenceWord === 'red' ? 'absent at red' : 'present at green'}).\n` +
  `- proof_type=demo: at green, capture the UI screenshot / :port evidence (a ui-tagged task still needs SOME artifact even on a non-demo proof_type).\n` +
  `Then conductor_gate(id="${locate.task_id}", action="approve", reason="<exact command/receipt + ${evidenceWord} summary>", fields=["gate_step","gate_state","to_step","auto_advanced","verifier"]) WITHOUT override - the fields projection keeps the verdict + verifier reason but DROPS the echoed task object (lean response; matters when many child drives run at once). Inspect: (a) ok:true -> verifier agreed, done. (b) ok:false, gate_state=failed AND a reason meaning the verifier saw NOTHING ("no diff in scope" / status=error / "no claims to verify") -> STRUCTURALLY BLIND to this checkout; recover via conductor_gate(action="approve", override=true, reason="<your real ${evidenceWord} proof; verifier blind to working tree>"). (c) ok:false because the verifier SAW the proof and disagreed -> do NOT override; set ok:false with the verifier reason in halt_reason. If the gate was ALREADY failed at step start (a prior blind run), go straight to (b). Report final to_step + gate_state.`

const HANDLERS = {
  review_previous_notes: (role = 'sm') => agent(
    `${preamble(role)}\n\nSTEP review_previous_notes.\n\n${ctx}\n\nWORK: recall prior decisions, related stories, and conventions for this area (memory_recall + brain_search first). Summarize what a builder must respect (existing patterns, prior bugs, gotchas) with file:line. ${advanceInstr('review_previous_notes', 'reviewed prior notes/decisions/memory')}`,
    { label: 'review_previous_notes', phase: 'Review notes', schema: STEP_SCHEMA }),

  draft_story: (role = 'sm') => agent(
    `${preamble(role)}\n\nSTEP draft_story (validation kind: story_complete - advisory, not a blocking gate).\n\n${ctx}\n\nWORK: draft a crisp story: user-facing goal, scope, and a numbered acceptance-criteria list the proof will pin. Keep it grounded in the requirements above; push anything unsupported into open questions.${DRY ? '' : ` Then set the ORACLE (goalbuddy completion contract) - the single OBSERVABLE signal that proves the user outcome (what the completion_proof must show at green_gate). RESPECT a proof_type the task ALREADY declares - if it is metric/artifact/demo/review, KEEP it (do NOT clobber it to "test"); the gates validate that oracle's own shape. Only DEFAULT proof_type="test" when the task has none (TDD is the default, not a forced choice). Record via task_update(id="${locate.task_id}", oracle="<observable signal>", proof_type="<test|metric|artifact|demo - match the oracle>").`} ${advanceInstr('draft_story', 'story + acceptance criteria drafted')}`,
    { label: 'draft_story', phase: 'Draft story', schema: STEP_SCHEMA }),

  story_gate: (role = 'sm') => agent(
    `${preamble(role)}\n\nSTEP story_gate (BLOCKING gate, RUBRIC-VERIFIED - validation story_complete). The server scores the task's plan_doc against the YAML rubric (required sections + every AC carries an id and an oracle). NO override on a compliant drive.\n\n${ctx}\n\nWORK: ${DRY ? 'Report that you would ensure plan_doc carries ## Summary / ## Requirements / ## Acceptance Criteria with AC-<n> ids + "- oracle:" markers, then conductor_gate(approve) WITHOUT override.' : `re-read the task via task_list(id="${locate.task_id}") (LEAN by-id read - returns just this task, never the whole board) and check its plan_doc is a rubric-compliant story: ## Summary, ## Requirements (FR-<n>/NFR-<n> ids), ## Acceptance Criteria (AC-<n> ids, each ending "- oracle: <observable check>"). If plan_doc is missing/non-compliant, WRITE the compliant story from the requirements above and persist it: task_update(id="${locate.task_id}", plan_doc=<story markdown>). Then call conductor_gate(id="${locate.task_id}", action="approve", reason="story rubric evidence: sections + AC ids + oracles present") WITHOUT override. If ok:false, the rubric reason names the exact gap - fix plan_doc and re-approve; NEVER override this gate.`} Report final to_step + gate_state.${telemetryInstr('story_gate')}`,
    { label: 'story_gate', phase: 'Story gate', schema: STEP_SCHEMA }),

  verify_plan: (role = 'sm') => agent(
    `${preamble(role)}\n\nSTEP verify_plan (validation kind: plan_coverage - advisory).\n\n${ctx}\n\nWORK: verify the plan covers EVERY requirement and names the concrete files/functions each will touch (brain_call_chain for blast radius first, disk only for gaps). If a requirement has no plan, that is a coverage gap - state it.\n\nPROACTIVE RESEARCH RUNG (size-gated, grounding-gated - AC6): for each non-trivial approach in the plan, classify its grounding. If the approach is UNGROUNDED in Brain AND grep - neither brain_search/memory_recall (the WHY) nor disk Grep/Read of existing source (the HOW) can ground the chosen technique, i.e. it is a NEW practice not yet present in this codebase - then this step BLOCKS: do NOT pass verify_plan until a cited WebSearch / best-practice pass exists for that approach. Set ok:false with the ungrounded approach named in halt_reason UNLESS you have run a WebSearch and can cite the source (url/title) that validates the best-practice. SIZE GATE: a trivial / one-line / pattern-already-in-repo approach is grounded by grep and needs NO web rung - only an ungrounded, non-trivial NEW practice triggers the blocking research requirement. Set source_tier="web" only when this rung actually fired; otherwise brain (why) or grep (how). ${advanceInstr('verify_plan', 'plan covers all requirements; files identified; ungrounded approaches research-cited')}`,
    { label: 'verify_plan', phase: 'Verify plan', schema: STEP_SCHEMA }),

  plan_gate: (role = 'sm') => agent(
    `${preamble(role)}\n\nSTEP plan_gate (BLOCKING gate, RUBRIC-VERIFIED - validation plan_coverage). The server scores: (i) every story AC id is covered by the plan_doc, (ii) plan_diagram is present and parses as mermaid (consult the mermaid-syntax skill), (iii) the plan_diagram's layer edges violate NO Brain-stored architecture principle - and an EMPTY principle store never passes (misfire guard). NO override on a compliant drive.\n\n${ctx}\n\nWORK: ${DRY ? 'Report that you would ensure plan_diagram exists/parses and principles are seeded, then conductor_gate(approve) WITHOUT override.' : `re-read the task via task_list(id="${locate.task_id}") (LEAN by-id read - returns just this task, never the whole board). Ensure plan_diagram is valid mermaid (mermaid-syntax skill; first line a bare diagram keyword) whose layer edges reflect the INTENDED architecture; persist via task_update(id="${locate.task_id}", plan_diagram=...) if missing. If the rubric reports no seeded principles, seed them ONCE via the MCP memory tools per services/arc_governance.py (seed_prism_principles writes them as memory data in domain "architecture-principles"). Then conductor_gate(id="${locate.task_id}", action="approve", reason="plan rubric evidence: AC coverage + parsing diagram + principle conformance") WITHOUT override. If ok:false, the rubric names the missing AC id / parse failure / violated principle - FIX the plan (never the rubric) and re-approve; a principle violation flagged HERE is the point: fix the plan BEFORE code.`} Report final to_step + gate_state.${telemetryInstr('plan_gate')}`,
    { label: 'plan_gate', phase: 'Plan gate', schema: STEP_SCHEMA }),

  write_failing_tests: (role = 'qa') => agent(
    `${preamble(role)}\n\nSTEP write_failing_tests (validation kind: red_with_trace).\n\n${ctx}\n\nWORK: write the SMALLEST set of tests that pin the acceptance criteria and FAIL today (red). Put them where the suite lives (find it brain-first, then on disk). CRITICAL - pin the USER-FACING INTEGRATION, not just unit contracts: a test that merely imports a service class or calls a method will pass even if that code is DEAD (no MCP verb, no API field, no hook, no UI). That exact gap has shipped false-greens. For any wiring/feature task, assert the real seam end-to-end - the MCP verb is reachable through the tool DISPATCHER (not just defined), the API route returns the new field, a queue/store survives across SEPARATE calls (not one in-memory instance), the hook actually dispenses, the UI actually renders. If your only red tests are unit-level, you have NOT pinned the feature. ${DRY ? 'Report the test files and assertions you would add and the command that would prove red.' : 'Run the exact test command, confirm it FAILS for the right reason, and capture the failing trace.'}${commitInstr('test', 'red scaffold')} Record the command + result in evidence. ${advanceInstr('write_failing_tests', 'failing tests landed + committed; red trace captured')}`,
    { label: 'write_failing_tests', phase: 'Red tests', schema: STEP_SCHEMA }),

  red_gate: (role = 'qa') => agent(
    `${preamble(role)}\n\nSTEP red_gate (BLOCKING gate). The verifier expects the suite to be RED (status=fail, tier0=fail) - that proves the tests bite before any implementation. On approve+pass the conductor auto-advances to implement_tasks.\n\n${ctx}\n\nWORK: ${DRY ? 'Report that you would run the test command (expecting RED), then call conductor_gate(approve) with the red trace, and the expected to_step (implement_tasks). Do not call anything.' : gateDoctrine('RED (failing for the right reason)', 'red')}${telemetryInstr('red_gate')}`,
    { label: 'red_gate', phase: 'Red gate', schema: STEP_SCHEMA }),

  implement_tasks: (role = 'dev') => agent(
    `${preamble(role)}\n\nSTEP implement_tasks (validation kind: green).\n\n${ctx}\n\nARCHITECTURE PRINCIPLES (governance, task 8579d49e): before editing, load the Brain-stored principles into your working context - memory_recall("architecture principles layer rules") (they live as memory data in domain "architecture-principles", seeded by services/arc_governance.py, e.g. domain must-not-depend-on infrastructure, models must-not-import services). Every edit must respect these machine-checkable layer rules; green_gate diffs the observed layer edges against them and annotates violations.\n\nWORK: make the SMALLEST change that turns the failing tests green. Chunk edits to ~30 lines. Reuse existing patterns (find them brain-first). If the change is user-visible, patch-bump PRISM_VERSION in the same change. WORKER CONTRACT (goalbuddy-ported): re-read this task via task_list(id="${locate.task_id}") (LEAN by-id read, not the whole board) - if it defines allowed_files, treat that allowlist as a HARD scope boundary: do NOT edit any file outside it. Honor stop_if - if you need a file outside allowed_files, the behavior is ambiguous, or verification fails twice, STOP: set ok:false and put the triggered stop_if condition in halt_reason rather than pushing through. ${DRY ? 'Report the files/edits you would make and the command that would prove green. Do not write.' : 'Run the test command and confirm it now PASSES; capture the green result in evidence.'}${commitInstr('feat', 'impl')} ${advanceInstr('implement_tasks', 'implementation complete + committed; targeted tests green')}`,
    { label: 'implement_tasks', phase: 'Implement', schema: STEP_SCHEMA }),

  verify_green_state: (role = 'qa') => agent(
    `${preamble(role)}\n\nSTEP verify_green_state (validation kind: green_full).\n\n${ctx}\n\nWORK: run the FULL relevant suite (not just the new tests) and verify every acceptance criterion is met. ${DRY ? 'Report the full command you would run.' : 'Capture the exact command + full-green result. If the ticket lists curl/UI verification, DO those against the running surface - tests-pass is not feature-works.'} If anything is red, set ok:false with the failure in halt_reason. ${advanceInstr('verify_green_state', 'full suite green; acceptance verified')}`,
    { label: 'verify_green_state', phase: 'Verify green', schema: STEP_SCHEMA }),

  green_gate: (role = 'lead') => agent(
    `${preamble(role)}\n\nSTEP green_gate (BLOCKING terminal gate).\n\n${ctx}\n\nWORK: this is the terminal sign-off. ${DRY ? 'Report that you would re-run the full suite (expecting GREEN), then conductor_gate(approve, override=true) with the full-green evidence, then mark the task done. Do not call anything.' : 'First RUN THE FULL SUITE yourself and confirm GREEN (capture the exact command + result). green_gate is terminal with no machine-sensible test, so call conductor_gate(id="' + locate.task_id + '", action="approve", override=true, reason="<full-green evidence: command + result + acceptance summary>"). Then RECORD THE COMPLETION PROOF (oracle contract): task_update(id="' + locate.task_id + '", status="done", proof_type="test", completion_proof="<the exact full-suite command + its green result + a one-line acceptance summary; receipt-backed evidence, NOT a placeholder>"). A real completion_proof clears the green_gate oracle / anti-busywork check (effort is not outcome).'}${DRY ? '' : ` Then - MANDATORY WHY-CAPTURE ON SUCCESS (AC5): a clean terminal pass is a DECISION, and the WHY must be written back to the source of truth, not just on failure (SELF_HEAL rung 4 covers only failures). Call memory_store(type="decision", ...) carrying the full WHY contract: the DECISION made, its RATIONALE, the REJECTED ALTERNATIVES (what you did NOT do and why), and concrete file:line refs to the change. memory_recall must surface this decision memory after a clean drive - a terminal success that records completion_proof but no decision memory has NOT written the WHY back.`} If the gate returns ok:false, set ok:false with the reason in halt_reason. Report to_step and gate_state.${telemetryInstr('green_gate')}`,
    { label: 'green_gate', phase: 'Green gate', schema: STEP_SCHEMA }),
}

// Role each step's agent persona carries - mirrors the HANDLERS defaults so
// the telemetry row's `role` matches the persona that actually ran the step.
const ROLE_BY_STEP = {
  review_previous_notes: 'sm', draft_story: 'sm', story_gate: 'sm',
  verify_plan: 'sm', plan_gate: 'sm',
  write_failing_tests: 'qa', red_gate: 'qa',
  implement_tasks: 'dev', verify_green_state: 'qa', green_gate: 'lead',
}

// -- Deterministic drive: one step at a time, halt on failure/gate-reject -
const trace = []
let halted = null
for (let i = startIdx; i < ORDER.length; i++) {
  const stepId = ORDER[i]
  const res = await HANDLERS[stepId]()
  trace.push(res)
  // Emit one agent-run telemetry row after this step's agent() returns -
  // serial path; the parallel fanout() wrapper routes the SAME emitter.
  // Timing is stamped server-side on ingest (workflow scripts forbid client clocks).
  await postAgentRun(res, { role: ROLE_BY_STEP[stepId], step: stepId })
  if (!res || res.ok !== true) {
    // DESYNC TOLERANCE (deterministic safety net; mirrors advanceInstr).
    // The conductor auto-advances a passing AGENT step to the next gate, so an
    // agent step's own conductor_advance can fail with "gate '...' is pending;
    // call gate_decide before advancing" even though its WORK succeeded - the
    // conductor is simply ahead. That previously halted the whole drive at
    // verify_green_state and left the task stuck at a pending green_gate
    // (memory mx-f886d9). Detect that EXACT case for a non-gate step and
    // CONTINUE to the gate handler instead of halting. A real gate-step
    // rejection, or any other failure (validation rejected / work failed),
    // still halts as before.
    const _reason = (res && res.halt_reason) || ''
    const _isGateStep = stepId.endsWith('_gate')
    const _pendingDesync = !_isGateStep && res && (
      String((res && res.gate_state) || '').toLowerCase() === 'pending' ||
      /\bpending\b|call gate_decide|already (?:past|at)\b/i.test(_reason)
    )
    if (_pendingDesync) {
      log(`Step "${stepId}" advance hit a pending gate (conductor ahead) - work done, continuing to the gate handler instead of halting.`)
      continue
    }
    halted = { at: stepId, reason: _reason || 'step reported ok:false', result: res }
    break
  }
  if (STOP_AFTER && stepId === STOP_AFTER) {
    log(`Reached stop_after="${STOP_AFTER}" - halting the drive as requested.`)
    break
  }
}

// -- Report --------------------------------------------------------------
const lastStep = trace.length ? trace[trace.length - 1].to_step : startStep
return {
  task_id: locate.task_id,
  title: locate.title,
  branch: locate.branch,
  dry_run: DRY,
  started_at_step: startStep,
  ended_at_step: lastStep,
  steps_driven: trace.map((t) => ({ step: t.step, ok: t.ok, to_step: t.to_step, gate_state: t.gate_state })),
  halted,
  trace,
}
