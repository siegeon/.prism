export const meta = {
  name: 'implement',
  description: 'Drive one PRISM task through the conductor-gated SDLC (review → story → verify_plan → red tests → red_gate → implement → verify_green → green_gate). Brain is the primary knowledge source; grepping source on disk is the fallback. The build-half companion to the `prototype` planning workflow.',
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

// ── Input normalization ────────────────────────────────────────────────
// args may arrive as a JSON string, a plain object, or a bare task id.
let _in = args
if (typeof _in === 'string') {
  try { _in = JSON.parse(_in) } catch { _in = { task_id: _in } }
}
_in = _in && typeof _in === 'object' ? _in : {}
const TASK_ID = (_in.task_id || _in.id || '').trim()
const DRY = _in.dry_run === true || _in.dry_run === 'true'
const STOP_AFTER = (_in.stop_after || '').trim() // e.g. 'red_gate' to halt the drive there
// The DRIVING Claude session id — sourced by the orchestrator from
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

// ── Conductor state machine (mirror of models/workflow.py:WORKFLOW_STEPS) ─
// Only red_gate and green_gate are blocking gates. The *_complete/_coverage
// validations on agent steps are advisory notes recorded on conductor_advance.
//
// VERIFIER-BLINDNESS NOTE: the conductor's gate verifier (VerifierService)
// scopes Tier-0 git to the MCP daemon's cwd, NOT this working checkout
// (project_context wires workspace=None; conductor._verify_gate passes no
// workspace). In a source-run dev topology the daemon often cannot see
// working-tree edits, so a gate's verifier returns status=error / "no diff
// in scope" even though the change is real. Gate handlers below treat that
// specific signal as STRUCTURALLY BLIND and recover via override using the
// agent's OWN executed test trace as evidence — but never override a gate
// whose verifier actually saw the diff and reported a genuine failure.
const ORDER = [
  'review_previous_notes', 'draft_story', 'verify_plan',
  'write_failing_tests', 'red_gate',
  'implement_tasks', 'verify_green_state', 'green_gate',
]

// ── Shared agent preamble ──────────────────────────────────────────────
const PRISM_TOOLS = 'You have PRISM MCP tools via ToolSearch. Load what you need, e.g. ToolSearch("select:mcp__prism__brain_search,mcp__prism__brain_understand,mcp__prism__brain_call_chain,mcp__prism__memory_recall,mcp__prism__task_list,mcp__prism__conductor_advance,mcp__prism__conductor_gate,mcp__prism__task_update"). Project slug is "prism".'

const KNOWLEDGE = [
  'KNOWLEDGE PROTOCOL — Brain is the primary repository, disk is the fallback:',
  '1. FIRST query the Brain: brain_search (try 3-4 query variants), brain_understand for a subgraph, brain_call_chain for blast radius, memory_recall for conventions/decisions.',
  '2. ONLY for what the Brain does not answer, fall back to Grep/Glob/Read on source under E:/.prism.',
  '3. Read before you cite. Every claim about code carries a concrete file:line. Never cite an unread source.',
].join('\n')

const CONVENTIONS = [
  'PRISM CONVENTIONS (hard rules):',
  '- Never commit to main/master/staging/develop. Work on the feature branch this workflow created.',
  '- File writes: max ~30 lines per edit operation; chunk larger writes.',
  '- Hooks are advisory (exit 0) — never block tool execution.',
  '- Destructive ops: validate paths, never -ErrorAction SilentlyContinue, no inline destructive PowerShell.',
  '- If the change is user-visible, patch-bump PRISM_VERSION in the same commit.',
].join('\n')

const dryNote = DRY
  ? '\n\nDRY-RUN MODE: Do NOT write files, do NOT run conductor_advance/conductor_gate/task_update, do NOT mutate anything. Only gather context and report exactly what you WOULD do. Treat the conductor transition as simulated and report the to_step you would expect.'
  : ''

function preamble(role) {
  return `${PRISM_TOOLS}\n\nYou are acting as the PRISM "${role}" persona inside the conductor SDLC.\n\n${KNOWLEDGE}\n\n${CONVENTIONS}${dryNote}`
}

// ── Agent-run telemetry emitter (task f4498190) ─────────────────────────
// ONE shared row builder + POST so the serial loop AND the parallel fanout
// wrapper emit an IDENTICAL row shape — no telemetry gap between the two
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
  try {
    await fetch(`${API_BASE}/api/agent-runs/ingest?project=prism`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(row),
    })
  } catch (e) {
    log(`agent-run telemetry POST failed (non-fatal): ${e && e.message}`)
  }
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

// ── Schemas ─────────────────────────────────────────────────────────────
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
    halt_reason: { type: 'string', description: 'set ONLY if the step failed or a gate rejected; empty otherwise' },
  },
}

// ── Phase: Locate ───────────────────────────────────────────────────────
phase('Locate')
const pick = TASK_ID
  ? `Read task id ${TASK_ID} via task_list and find the row whose id matches.`
  : 'No task id was supplied. Call task_next to choose the highest-priority unblocked task, then read its row via task_list.'

const locate = await agent(
  `${preamble('analyst')}\n\nGOAL: locate the task and orient before the conductor drive.\n\n${pick}\n\nThen:\n- Report current_step (workflow_step) and gate_state exactly as stored.\n- Build a brain-first context_summary of the subsystem this task touches (brain_search/brain_understand first, disk grep only for gaps), with file:line refs.\n- Distill the task description + any acceptance criteria into a discrete \`requirements\` list — each item independently testable.\n- BRANCH: report the git branch the work will land on. If currently on main/master/staging/develop${DRY ? ', report the branch name you WOULD create (do not create it).' : ', create a feature branch off main (e.g. feat/<task-slug>) and switch to it, then report its name.'}\n${DRY ? '' : '- REQUIRED FIRST ACTION: immediately call task_update(id, status="in_progress") — do this before anything else so the tasks/kanban view shows the task as actively worked (not stranded in the pending column) while the SDLC runs.'}${SID && !DRY ? `\n- IMMEDIATELY AFTER that first action, call task_link_session(task_id="${TASK_ID}", session_id="${SID}") to tie this driving session to the task (explicit session_id — never the request_id default).` : ''}\n\nReturn the structured locate result.`,
  { label: 'locate', phase: 'Locate', schema: LOCATE_SCHEMA })

const startStep = locate.current_step && ORDER.includes(locate.current_step)
  ? locate.current_step
  : 'review_previous_notes'
const startIdx = ORDER.indexOf(startStep)
log(`Task "${locate.title}" is at "${startStep}" (gate=${locate.gate_state}) on branch ${locate.branch}. Driving${DRY ? ' (DRY-RUN)' : ''} from there.`)

// ── Per-step handler prompts ────────────────────────────────────────────
const ctx = `TASK: ${locate.title} (id ${locate.task_id})\nBRANCH: ${locate.branch}\nREQUIREMENTS:\n- ${locate.requirements.join('\n- ')}\n\nCONTEXT (brain-first):\n${locate.context_summary}`

function advanceInstr(stepId, validationHint) {
  // GUARDED: thread session_id=SID into the emitted conductor_advance call so
  // every non-gate step refreshes the task<->session link. When SID is empty
  // the arg is dropped entirely (byte-identical to today's no-session call).
  const sidArg = SID ? `, session_id="${SID}"` : ''
  return `After the work, call conductor_advance(id="${locate.task_id}"${sidArg}, validation="${validationHint}") to leave "${stepId}". Report the returned to_step and gate_state. If the call returns ok:false, set ok:false and put its reason in halt_reason.`
}

// Commit the step's changes on the feature branch so (a) the eventual PR is
// real and (b) the gate verifier has a committed diff IF it can see this tree.
const commitInstr = (kind, scope) => DRY
  ? ''
  : ` Then COMMIT this step's changes on the feature branch (NEVER on main): \`git add <the files you touched> && git commit -m "${kind}(${scope}): <one-line> [conductor:${locate.task_id.slice(0, 8)}]"\`. Report the commit short-sha in evidence.`

// Gate recovery doctrine, shared by both blocking gates. The agent's OWN
// executed test command is the ground truth; the conductor verifier may be
// blind to this checkout (see VERIFIER-BLINDNESS NOTE).
const gateDoctrine = (expectWord, evidenceWord) =>
  `GATE DOCTRINE: First run the real test command yourself and capture the trace — it must be ${expectWord}. Then call conductor_gate(id="${locate.task_id}", action="approve", reason="<exact command + ${evidenceWord} summary>") WITHOUT override. Inspect the result: (a) ok:true -> the verifier saw the diff and agreed, done. (b) ok:false with gate_state=failed AND a reason that means the verifier saw NOTHING ("no diff in scope" / status=error / "no claims to verify") -> the verifier is STRUCTURALLY BLIND to this checkout; recover by re-calling conductor_gate(action="approve", override=true, reason="<your real command + ${evidenceWord} trace; verifier blind to working tree>"). (c) ok:false because the verifier SAW the diff and it disagreed with your local result -> do NOT override; set ok:false and put the verifier reason in halt_reason. If the gate was ALREADY gate_state=failed when this step started (a prior blind run), go straight to the override path in (b). Report final to_step + gate_state.`

const HANDLERS = {
  review_previous_notes: (role = 'sm') => agent(
    `${preamble(role)}\n\nSTEP review_previous_notes.\n\n${ctx}\n\nWORK: recall prior decisions, related stories, and conventions for this area (memory_recall + brain_search first). Summarize what a builder must respect (existing patterns, prior bugs, gotchas) with file:line. ${advanceInstr('review_previous_notes', 'reviewed prior notes/decisions/memory')}`,
    { label: 'review_previous_notes', phase: 'Review notes', schema: STEP_SCHEMA }),

  draft_story: (role = 'sm') => agent(
    `${preamble(role)}\n\nSTEP draft_story (validation kind: story_complete — advisory, not a blocking gate).\n\n${ctx}\n\nWORK: draft a crisp story: user-facing goal, scope, and a numbered acceptance-criteria list that the failing tests will pin. Keep it grounded in the requirements above; push anything unsupported into open questions.${DRY ? '' : ` Then set the ORACLE (goalbuddy completion contract) — the single OBSERVABLE signal that proves the user outcome (what the completion_proof must show at green_gate). If the task has none, record it: task_update(id="${locate.task_id}", oracle="<observable signal>", proof_type="test").`} ${advanceInstr('draft_story', 'story + acceptance criteria drafted')}`,
    { label: 'draft_story', phase: 'Draft story', schema: STEP_SCHEMA }),

  verify_plan: (role = 'sm') => agent(
    `${preamble(role)}\n\nSTEP verify_plan (validation kind: plan_coverage — advisory).\n\n${ctx}\n\nWORK: verify the plan covers EVERY requirement and names the concrete files/functions each will touch (brain_call_chain for blast radius first, disk only for gaps). If a requirement has no plan, that is a coverage gap — state it. ${advanceInstr('verify_plan', 'plan covers all requirements; files identified')}`,
    { label: 'verify_plan', phase: 'Verify plan', schema: STEP_SCHEMA }),

  write_failing_tests: (role = 'qa') => agent(
    `${preamble(role)}\n\nSTEP write_failing_tests (validation kind: red_with_trace).\n\n${ctx}\n\nWORK: write the SMALLEST set of tests that pin the acceptance criteria and FAIL today (red). Put them where the suite lives (find it brain-first, then on disk). CRITICAL — pin the USER-FACING INTEGRATION, not just unit contracts: a test that merely imports a service class or calls a method will pass even if that code is DEAD (no MCP verb, no API field, no hook, no UI). That exact gap has shipped false-greens. For any wiring/feature task, assert the real seam end-to-end — the MCP verb is reachable through the tool DISPATCHER (not just defined), the API route returns the new field, a queue/store survives across SEPARATE calls (not one in-memory instance), the hook actually dispenses, the UI actually renders. If your only red tests are unit-level, you have NOT pinned the feature. ${DRY ? 'Report the test files and assertions you would add and the command that would prove red.' : 'Run the exact test command, confirm it FAILS for the right reason, and capture the failing trace.'}${commitInstr('test', 'red scaffold')} Record the command + result in evidence. ${advanceInstr('write_failing_tests', 'failing tests landed + committed; red trace captured')}`,
    { label: 'write_failing_tests', phase: 'Red tests', schema: STEP_SCHEMA }),

  red_gate: (role = 'qa') => agent(
    `${preamble(role)}\n\nSTEP red_gate (BLOCKING gate). The verifier expects the suite to be RED (status=fail, tier0=fail) — that proves the tests bite before any implementation. On approve+pass the conductor auto-advances to implement_tasks.\n\n${ctx}\n\nWORK: ${DRY ? 'Report that you would run the test command (expecting RED), then call conductor_gate(approve) with the red trace, and the expected to_step (implement_tasks). Do not call anything.' : gateDoctrine('RED (failing for the right reason)', 'red')}`,
    { label: 'red_gate', phase: 'Red gate', schema: STEP_SCHEMA }),

  implement_tasks: (role = 'dev') => agent(
    `${preamble(role)}\n\nSTEP implement_tasks (validation kind: green).\n\n${ctx}\n\nWORK: make the SMALLEST change that turns the failing tests green. Chunk edits to ~30 lines. Reuse existing patterns (find them brain-first). If the change is user-visible, patch-bump PRISM_VERSION in the same change. WORKER CONTRACT (goalbuddy-ported): re-read this task via task_list — if it defines allowed_files, treat that allowlist as a HARD scope boundary: do NOT edit any file outside it. Honor stop_if — if you need a file outside allowed_files, the behavior is ambiguous, or verification fails twice, STOP: set ok:false and put the triggered stop_if condition in halt_reason rather than pushing through. ${DRY ? 'Report the files/edits you would make and the command that would prove green. Do not write.' : 'Run the test command and confirm it now PASSES; capture the green result in evidence.'}${commitInstr('feat', 'impl')} ${advanceInstr('implement_tasks', 'implementation complete + committed; targeted tests green')}`,
    { label: 'implement_tasks', phase: 'Implement', schema: STEP_SCHEMA }),

  verify_green_state: (role = 'qa') => agent(
    `${preamble(role)}\n\nSTEP verify_green_state (validation kind: green_full).\n\n${ctx}\n\nWORK: run the FULL relevant suite (not just the new tests) and verify every acceptance criterion is met. ${DRY ? 'Report the full command you would run.' : 'Capture the exact command + full-green result. If the ticket lists curl/UI verification, DO those against the running surface — tests-pass is not feature-works.'} If anything is red, set ok:false with the failure in halt_reason. ${advanceInstr('verify_green_state', 'full suite green; acceptance verified')}`,
    { label: 'verify_green_state', phase: 'Verify green', schema: STEP_SCHEMA }),

  green_gate: (role = 'lead') => agent(
    `${preamble(role)}\n\nSTEP green_gate (BLOCKING terminal gate).\n\n${ctx}\n\nWORK: this is the terminal sign-off. ${DRY ? 'Report that you would re-run the full suite (expecting GREEN), then conductor_gate(approve, override=true) with the full-green evidence, then mark the task done. Do not call anything.' : 'First RUN THE FULL SUITE yourself and confirm GREEN (capture the exact command + result). green_gate is terminal with no machine-sensible test, so call conductor_gate(id="' + locate.task_id + '", action="approve", override=true, reason="<full-green evidence: command + result + acceptance summary>"). Then RECORD THE COMPLETION PROOF (oracle contract): task_update(id="' + locate.task_id + '", status="done", proof_type="test", completion_proof="<the exact full-suite command + its green result + a one-line acceptance summary; receipt-backed evidence, NOT a placeholder>"). A real completion_proof clears the green_gate oracle / anti-busywork check (effort is not outcome).'} If the gate returns ok:false, set ok:false with the reason in halt_reason. Report to_step and gate_state.`,
    { label: 'green_gate', phase: 'Green gate', schema: STEP_SCHEMA }),
}

// Role each step's agent persona carries — mirrors the HANDLERS defaults so
// the telemetry row's `role` matches the persona that actually ran the step.
const ROLE_BY_STEP = {
  review_previous_notes: 'sm', draft_story: 'sm', verify_plan: 'sm',
  write_failing_tests: 'qa', red_gate: 'qa',
  implement_tasks: 'dev', verify_green_state: 'qa', green_gate: 'lead',
}

// ── Deterministic drive: one step at a time, halt on failure/gate-reject ─
const trace = []
let halted = null
for (let i = startIdx; i < ORDER.length; i++) {
  const stepId = ORDER[i]
  const res = await HANDLERS[stepId]()
  trace.push(res)
  // Emit one agent-run telemetry row after this step's agent() returns —
  // serial path; the parallel fanout() wrapper routes the SAME emitter.
  // Timing is stamped server-side on ingest (workflow scripts forbid client clocks).
  await postAgentRun(res, { role: ROLE_BY_STEP[stepId], step: stepId })
  if (!res || res.ok !== true) {
    halted = { at: stepId, reason: (res && res.halt_reason) || 'step reported ok:false', result: res }
    break
  }
  if (STOP_AFTER && stepId === STOP_AFTER) {
    log(`Reached stop_after="${STOP_AFTER}" — halting the drive as requested.`)
    break
  }
}

// ── Report ──────────────────────────────────────────────────────────────
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
