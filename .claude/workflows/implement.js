export const meta = {
  name: 'implement',
  description: 'Drive one PRISM task to a genuinely-evidenced green_gate as a SERVER-DRIVEN QUEUE: loop on the single verb conductor_work, do exactly job["instructions"], produce job["expected_proof"]. The server owns WORKFLOW_STEPS - this script never names a step and never clears a gate (distinct-actor). Work lands in the task\'s OWN git worktree (the path the gate verifier reads), the call graph sets the blast radius, and an oversized slice decomposes into conductor-driven CHILD tasks (skeleton first, then the disjoint slices concurrently) whose shared seam is named ONCE up front and which the parent then assembles rather than rebuilds. Every step runs at the capability tier PRISM assigns its role, so frontier reasoning is spent on planning and gate judgment rather than on fetch-and-report. The build-half companion to the `prototype` planning workflow.',
  whenToUse: 'Run to actually WORK a task through PRISM\'s conductor. Invoke as Workflow({name:"implement", args:{task_id:"<uuid>"}}). Omit task_id to let the server pull the next unblocked task. Pass {dry_run:true} to trace read-only (no conductor mutations, no writes), {stop_after:"red_gate"} to halt at a named step, {max_children:N} to cap plan-time decomposition (0 disables it), or {gate_wait_s:N} to change how long a gate waits for a distinct seat before reporting who must act (default 240). Pass {step_budget_s:N} to change the wall-clock ceiling on a SINGLE step (default 1800, clamped 60..14400; known-slow steps such as verify_green_state get a multiple of it) - on exhaustion the drive halts itself and reports the step, the budget and how long it actually ran, keeping any work already committed. A non-default topology passes api_base pointing at its own web port; the default is the canonical release port.',
  // Each phase declares the MODEL it runs at, so the tier is visible in the
  // progress UI instead of being a silent choice inside the script. PRISM
  // publishes a tier per SDLC role (Steward=frontier, Verifier=balanced,
  // Builder=fast); frontier is spent only where the work is judgment.
  phases: [
    { title: 'Claim', detail: 'Instant PRISM visibility: in_progress + session link + first heartbeat', model: 'haiku' },
    { title: 'Pre-flight', detail: 'Fail fast: branch, clock, daemon identity, deps', model: 'haiku' },
    { title: 'Locate', detail: 'Task + conductor state; brain-first context; claim the task worktree', model: 'sonnet' },
    { title: 'Graph', detail: 'Call-graph blast radius -> allowed_files + neighbouring suites', model: 'sonnet' },
    { title: 'Decompose', detail: 'Oversized slice -> disjoint child tasks, one named seam, skeleton first', model: 'opus' },
    { title: 'Children', detail: 'Skeleton first, then the disjoint slices CONCURRENTLY', model: 'sonnet' },
    { title: 'Drive', detail: 'conductor_work loop: do the job, report the proof (model per job.role)' },
    { title: 'Gate', detail: 'Produce evidence, then WAIT for the distinct seat', model: 'opus' },
    { title: 'Settle', detail: 'Terminal receipt, evidence citations, and whether it actually shipped', model: 'haiku' },
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
// Plan-time decomposition cap. A slice whose blast radius spans disjoint
// surfaces is split into CHILD tasks (parent_id = the driven task) that each
// get their OWN conductor drive - the epic lesson from 0784729f: 5 green
// slices whose collaborators were all mocked assembled into a dead feature.
// 0 disables decomposition entirely (single-slice drive, today's behavior).
const MAX_CHILDREN = Number.isFinite(Number(_in.max_children))
  ? Math.max(0, Math.min(6, Number(_in.max_children)))
  : 3
// Bounded wait for a DISTINCT gate seat (machine adjudicator / human) to
// decide. The drive never clears its own gate, so this is how it makes
// progress: produce the evidence, then poll. Seconds.
const GATE_WAIT_S = Number.isFinite(Number(_in.gate_wait_s))
  ? Math.max(30, Math.min(900, Number(_in.gate_wait_s)))
  : 240
// <step-budget>
// WALL-CLOCK BOUND FOR ONE STEP (task dcbd284f). The stall guard in the drive
// loop counts HAND-BACKS and is evaluated BEFORE the awaited step call, so a
// step whose subprocess wedges keeps the loop on its first iteration and is
// structurally invisible to it. That is how a drive burned 3.9h, ~2.5 of them
// inside one wedged step, with nothing telling it to stop.
// This script has NO clock: the two client-clock reads are banned in
// .claude/workflows/*.js (spelled apart on purpose in this very sentence --
// the pre-flight's own grep for the literal patterns has no comment
// exception, and task 3a3f90da's relaunch found an earlier, plainly-spelled
// version of this comment tripping its own ban: writing the banned text out
// even as documentation blocked every drive's pre-flight). The pre-flight
// below fails the drive on any real match, and the runtime throws on them
// because they break resume. So the number is
// DECLARED here and ENFORCED BY THE STEP AGENT, which has a real clock through
// bash - exactly the shape GATE_WAIT_S already uses for a gate seat. Seconds.
// Kept pure and side-effect free: the pinned suite executes this block under
// node with a stubbed `_in` and asserts the values it returns.
const STEP_BUDGET_S = Number.isFinite(Number(_in.step_budget_s))
  ? Math.max(60, Math.min(14400, Number(_in.step_budget_s)))
  : 1800
// Steps that run a REAL suite need more room than a step that just reports.
// Named explicitly so the escalation lives in the code path that picks the
// number rather than in an unused constant.
const SLOW_STEPS = { verify_green_state: 3, implement_tasks: 2 }
function stepBudgetFor(step) {
  return Math.min(14400, STEP_BUDGET_S * (SLOW_STEPS[step] || 1))
}
// A budget halt is a HALT, never a failed run: work already committed may be
// green, and the caller still gets steps_driven + trace beside this object.
// It deliberately carries no status/error field for that reason.
function budgetHalt(step, budgetS, elapsedS, retries) {
  const r = Number(retries) || 0
  return {
    at: step,
    kind: 'agent',
    reason: `step budget exhausted: "${step}" ran ${elapsedS}s against a `
      + `budget of ${budgetS}s`
      + (r ? `, retrying ${r}x inside the step` : '')
      + '. The drive stopped itself instead of waiting longer. Anything '
      + 'already committed is kept and reported below.',
    gate_state: '',
  }
}
// </step-budget>
// The DRIVING Claude session id - sourced by the orchestrator from
// CLAUDE_CODE_SESSION_ID and passed in via args (workflow JS has no
// env/process access). Threaded into task_link_session + conductor_advance
// so the task<->session JOIN resolves. GUARDED: empty SID => today's behavior.
const SID = (_in.session_id || '').trim()
// Telemetry run id for the agent-run spine (task f4498190). One drive of this
// workflow == one run_id; every step's agent() emits a row under it. Sourced
// from args when the orchestrator supplies one, else derived from SID/task.
const RUN_ID = (_in.run_id || _in.runId || SID || TASK_ID || 'run-adhoc').trim()
// Where the agent-run telemetry POSTs land AND the conductor daemon the
// pre-flight probes. Defaults to the CANONICAL release web port (7778, the
// port `prism start` binds and every customer runs). A dev instance on a
// non-default port passes args.api_base (e.g. http://127.0.0.1:8888 for the
// 8887/8888 dev topology). Never hardcode a dev port here - this script
// ships. The MCP daemon serves /api/* on the same FastAPI app.
const API_BASE = (_in.api_base || 'http://127.0.0.1:7778').replace(/\/$/, '')

// -- THE SERVER OWNS THE SEQUENCE ----------------------------------------
// This script no longer mirrors models/workflow.py:WORKFLOW_STEPS. It loops
// on the ONE verb `conductor_work` and does whatever job the server hands
// back (api/conductor_flow.py:_job builds it: {task_id, step, kind, role,
// gate_state, gate_reason, instructions, expected_proof, doctrine,
// contract:{allowed_files, verify, stop_if}}). Naming a step here is how the
// old drive silently desynced from the real state machine.
//
// WORKSPACE (this is the fix for the whole class of "verifier blind" and
// "red anchor stranded" failures): conductor_work's START call runs
// task_workspace.ensure_workspace(task_id) and returns `workspace` - a REAL
// git worktree of this repo, on its own branch, under
// data_dir/task_workspaces/<task_id>. api/conductor_flow.py:262 says it
// outright: "the verifier reads the same path, so the worker's committed
// tests are what gates check." The old drive cut a feature branch in the
// SHARED checkout instead, so the verifier genuinely could not see the diff
// and every gate needed override=true to pass. Working IN the task worktree
// deletes the need for override, and it is what makes the red anchor land on
// a tests-only commit the machine seat can replay.
//
// GATES ARE NOT OURS TO CLEAR. flow_report refuses a gate report whose
// session produced any prior step (the distinct-actor rule, enforced by
// session identity at api/conductor_flow.py:428). So the drive NEVER reports
// a gate outcome. It makes the evidence real and WAITS for a distinct seat:
//   story_gate / plan_gate -> pure YAML rubric (services/arc_governance.py)
//     auto-clears on a machine PASS, and the ~20s adjudicator sweep re-scores
//     a gate that parked. Fix plan_doc/plan_diagram, then wait.
//   red_gate / green_gate  -> the machine adjudicator seat decides on a FRESH
//     passing EvidenceReceipt, but only where the environment opted in via
//     PRISM_GATE_ADJUDICATOR_INTERVAL. Otherwise a human clicks.
//   green_gate on proof_type=demo|review -> HUMAN ONLY, by owner rule
//     eaafdf75. adjudicate_green_gate deliberately returns None for these.
//     Do NOT "unblock" one by editing the oracle or flipping proof_type -
//     that games the single sign-off. Make the click GROUNDED and stop.
const GATE_STEPS = ['story_gate', 'plan_gate', 'red_gate', 'green_gate']
// OWNER RULE 2026-08-04 (mx-1eb0a9), enforced HERE rather than in CLAUDE.md
// prose: the human stops at EXACTLY TWO gates. Everything else belongs to a
// machine seat, and a machine seat that will not decide is a DEFECT to
// diagnose - never a click to hand the owner. Owner, verbatim: "i do not want
// to be stoped on the approve red gate for the tests, that is for the
// adjuctor or whatever, you stop at approve the plan, and approve the final
// green state."
const HUMAN_GATES = ['plan_gate', 'green_gate']
const MACHINE_GATES = GATE_STEPS.filter((g) => !HUMAN_GATES.includes(g))
function isHumanGate(step) { return HUMAN_GATES.includes(step) }
// Safety bound on the pull loop - the server decides when it is done; this
// only stops a pathological ping-pong (a step that never advances).
const MAX_JOBS = 24

// -- Model tiering (task: drive cost) ------------------------------------
// PRISM assigns every SDLC step a ROLE with a capability TIER and states the
// mapping on every MCP connection: Steward(sm)=frontier, Verifier(qa)=balanced,
// Builder(dev)=fast. This script used to ignore it entirely - every agent()
// inherited the session model, so a measured run (wf_15798a7d-99a, 2026-08-02)
// put ALL 11 agents on claude-opus-5 for 74.7 min and 1.25M tokens on a
// two-file change. Honouring the tier PRISM already publishes is the single
// largest lever on drive cost, and it is not a quality trade: the frontier
// model is kept for exactly the two things that need judgment - planning and
// deciding a gate.
const TIER_MODEL = { frontier: 'opus', balanced: 'sonnet', fast: 'sonnet', mechanical: 'haiku' }
const ROLE_TIER = { sm: 'frontier', qa: 'balanced', dev: 'fast' }
// Steps whose work is fetch-and-report, not judgment. A read-back of server
// state or a git log does not need a frontier turn.
const MECHANICAL_STEPS = ['pre-flight', 'settle']
function modelFor(role, step, isGate) {
  // A gate is a JUDGMENT call and the one place a cheap model would actually
  // cost something - never tier it down.
  if (isGate) return TIER_MODEL.frontier
  if (MECHANICAL_STEPS.includes(step)) return TIER_MODEL.mechanical
  const tier = ROLE_TIER[String(role || '').toLowerCase()]
  return TIER_MODEL[tier] || TIER_MODEL.balanced
}

// -- Shared agent preamble ----------------------------------------------
const PRISM_TOOLS = 'You have PRISM MCP tools (mcp__prism__*) served DIRECTLY on the `drive` tool profile - they are already loaded; NEVER call ToolSearch to load them (task 9b0f7c4b: that preload was a harness tool load costing more than a Bash grep). FIRST ACTION, before any Bash/Read/Grep retrieval (task 3a3f90da FR-3): call the PRISM verb you need directly. Project slug is "prism". `conductor_work` is THE drive verb - conductor_advance, conductor_gate and workflow_state are superseded admin verbs and must not be used on a drive.'

const KNOWLEDGE = [
  'KNOWLEDGE PROTOCOL - Brain is the primary repository, disk is the fallback:',
  '1. FIRST query the Brain: brain_search (try 3-4 query variants), brain_understand for a subgraph, brain_call_chain for blast radius, memory_recall for conventions/decisions.',
  '2. ONLY for what the Brain does not answer, fall back to Grep/Glob/Read on source under E:/.prism. A Bash call that shells out to grep, rg, sed, awk, find, cat, head or tail IS that same disk fallback and is counted as one (task 3a3f90da FR-1; memory mx-53efa3: a drive made 832 Bash-shelled grep calls and 0 Grep-tool calls, so a Grep-tool-only count hides the real ratio). Never open Bash for retrieval before the Brain has been asked.',
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
  '- TASK-BOARD HYGIENE (hard): NEVER call task_create for a ROOT/parent task (parent_id="" or omitted). You are driving ONE task; spawning a sibling on the board is always wrong. The ONLY task_create permitted is a CHILD of the task being driven - it MUST pass parent_id="<the driven task id>" so it stays off the root board and is reached from the parent detail page. SEARCH BEFORE YOU FILE: run task_list (and brain_search) for the defect first - the board very often already owns it, and a near-duplicate is worse than no ticket because it splits the fix across two oracles. On 2026-08-06 a main-thread session re-diagnosed the readiness-vs-decider divergence from scratch and filed a new ticket, while 5c61e0e6 had owned it since 2026-08-02 with its test path already picked. If an existing ticket covers it, add the new instance to THAT one instead.',
  '- WORK IN THE TASK WORKTREE, never the shared checkout. conductor_work\'s start call returns workspace.path (a real git worktree under data_dir/task_workspaces/<task_id>). Every edit, every test run, and every commit for this task happens with `git -C <workspace.path>` / cwd=<workspace.path>. The gate verifier reads THAT path - work done in E:/.prism is invisible to it. This shared checkout also holds OTHER sessions\' uncommitted work: never `git add -A`, never `git checkout -- <file>`, and never commit from it on this drive.',
  '- Re-read the branch in the SAME command as every commit (`git -C <ws> rev-parse --abbrev-ref HEAD && git -C <ws> commit ...`) - this tree is shared and HEAD moves under you.',
  '- Tag every commit with the task trailer `[task:<task-id-8>]` (the Delivery pipeline reads trailers), and commit the FAILING TESTS as a TESTS-ONLY commit before any implementation commit - the red machine seat anchors to that commit.',
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

// ENVIRONMENT COLLISION (cwd-isolation leak) doctrine - a Claude Code
// HARNESS/PLATFORM bug, never a PRISM or task defect. Observed TWICE in one
// real session: a step subagent's Bash tool was unconditionally refused mid-
// step with "This session is isolated in the worktree <some worktree>, but
// this command's working directory resolved to the shared checkout...
// Refusing to run it there" - and the worktree NAMED belonged to a SIBLING
// agent in the same session (once the MAIN session itself), never to the
// blocked subagent's own context, which was never told to use that worktree
// or any worktree at all. ExitWorktree also refused to help: "cannot be
// called from a subagent with a cwd override... This agent is already
// isolated." So a step subagent is structurally UNABLE to clear this itself.
// Platform feedback on the harness bug has already been filed separately -
// this constant is the PRISM-side mitigation only: recognize the signature,
// stop cleanly, report it as a distinct greppable halt, never work around it.
// Kept as its OWN constant (not folded silently into SELF_HEAL's prose) so it
// can be injected into BOTH the worker preamble (SELF_HEAL, below - reaches
// every workerPrompt/decompose/child-driver/locate/graph/settle agent) AND
// gatePrompt directly (gate agents do NOT run through preamble()/SELF_HEAL at
// all - see gatePrompt below) - every step agent, worker or gate, must
// recognize this exact signature the same way.
const ENV_COLLISION_DOCTRINE = [
  'ENVIRONMENT COLLISION (cwd-isolation leak) - recognize this EXACT signature: a Bash call refused with a message starting "This session is isolated in the worktree ..." naming a worktree you were NEVER told to use (not your own job/workspace path from this prompt).',
  '- This is a KNOWN Claude Code harness/platform bug (isolation state leaking across sibling agents in one session), NEVER a real task failure and NEVER something to work around cleverly. Do not retry the command via another tool. Do not try to fix the isolation state yourself - ExitWorktree structurally refuses a subagent with a cwd override ("cannot be called from a subagent... already isolated"); you cannot clear another agent\'s isolation from inside your own context, full stop.',
  '- STOP the step immediately - do not climb the SELF-HEAL ladder for this case, there is no fix available to you. Report ok:false with halt_reason starting with the EXACT fixed prefix "ENVIRONMENT COLLISION (cwd-isolation leak): " followed by a one-line detail (the worktree path named in the refusal, the command you were running). That fixed prefix is what makes the halt instantly greppable/recognizable, instead of buried inside a long generic "couldn\'t complete this step" narrative.',
  '- RECOVERY is always a FRESH relaunch of the whole drive, once no other agent in the session is mid-EnterWorktree - never resumeFromRunId (this repo\'s own CLAUDE.md: cached pre-flight verdicts replay stale). This is not yours to retry; report it and stop.',
].join('\n')

// SELF-HEAL doctrine (implement-workflow reliability). Injected into every step
// agent's preamble so a failed step / genuine gate-reject / runtime error
// triggers a knowledge-ladder climb instead of a dead halt. NOTE: the RESEARCH
// rung is WebSearch/WebFetch today; Context7 MCP (clean library docs) is a
// not-yet-wired follow-up.
const SELF_HEAL = [
  'SELF-HEAL DOCTRINE - when a step fails, a gate GENUINELY rejects (NOT the verifier-blind case above, and NOT the environment collision below), or a tool/runtime error fires, climb the ladder before giving up:',
  '1. PRISM-FIRST: brain_search + memory_recall for a known resolution. PRISM is the knowledge AND time authority - it server-stamps run timestamps; never reach for a client clock.',
  '2. RESEARCH: if PRISM has no answer, research best practices via WebSearch/WebFetch and cite sources.',
  '3. APPLY the smallest fix.',
  '4. RECORD: memory_store(type="failure", with file:line + the fix) so PRISM HAS THE ANSWER next time - failures become memories.',
  '',
  ENV_COLLISION_DOCTRINE,
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
// `role` comes from the JOB the server handed back (job.role) - this script
// no longer keeps its own step->role map, because that map was a second copy
// of WORKFLOW_STEPS and drifted from it.
function telemetryInstr(stepId, role) {
  if (DRY) return ''
  role = role || 'agent'
  const tid = (typeof locate !== 'undefined' && locate.task_id) || TASK_ID
  return `\n\nTELEMETRY (step contract - REQUIRED LAST ACTION): POST your agent-run row with Bash curl (use 127.0.0.1, NEVER localhost): \`curl -s -m5 -X POST '${API_BASE}/api/agent-runs/ingest?project=prism' -H 'Content-Type: application/json' -d '<ROW>'\` where <ROW> is the JSON object {"run_id":"${RUN_ID}","workflow_name":"implement","task_id":"${tid}","session_id":"${SID}","agent_id":"${RUN_ID}:${stepId}","role":"${role}","step":"${stepId}","ok":<your step ok, true|false>,"verdict_summary":"<one-line validation/evidence summary>"}. The response must be {"ok":true,...} - quote that receipt in evidence. A failed POST is non-fatal: note it and continue (timing is stamped server-side on ingest).\n\nDO NOT add a "gate_state" field to that row, and NEVER send "gate_state":"passed". You are a PRODUCING actor: the gate state belongs to the conductor, and a producer POSTing a passing gate state into the audit spine is self-approval by another route - the exact hole task 682b7e48 exists to close, and it trips the self-approval classifier. Telemetry records what YOU did, never a verdict.

NEVER include a "tokens" field in that row either - token accounting is harness-tracked server-side from the real transcript, never self-reported by the model being metered. Omit the key entirely.`
}

// -- Drive-liveness heartbeat (task e3b7ebf6) -----------------------------
// telemetryInstr fires ONCE, at step end - useless for a step that runs
// past the conductor's 120s/90s alarm windows while genuinely healthy (the
// "stalled must mean the owner has something to do" regression). This is
// the mid-step counterpart: instructs the step agent to POST a liveness
// beat every couple of minutes DURING the step, carrying real progress
// evidence (a monotonic work_units counter) so a wedged/looping process
// cannot pass for driving just by re-pinging. Optional and best-effort -
// a missed beat never blocks the step; activity_for simply falls back to
// the existing motion/quiet signals.
function heartbeatInstr(job) {
  if (DRY) return ''
  const tid = locate.task_id
  const step = job.step
  return `\n\nHEARTBEAT (drive liveness - REQUIRED, not optional; while you work, the owner's board computes "stalled" from step boundaries alone, and a beat is the ONLY signal that reaches it mid-step). Post your FIRST beat immediately after your first tool call of this step, then re-beat after roughly every 5-8 tool calls (at least every ~2 minutes of work): \`curl -s -m5 -X POST '${API_BASE}/api/drive-heartbeat/beat?project=prism' -H 'Content-Type: application/json' -d '{"task_id":"${tid}","step":"${step}","elapsed_s":<seconds since you started this step>,"last_tool":"<the tool you just ran>","work_units":<a counter you increment each beat, e.g. total tool calls so far>}'\`. work_units MUST strictly increase and reflect REAL progress - two beats with an unchanged counter are treated as a wedged/looping process, not a driving one. A missed or failed beat is non-fatal (never let it block the step), but skipping beats entirely makes the owner's board cry "stalled" over healthy work - that is a report-quality defect on YOUR step.`
}

// -- Agent-run telemetry emitter (task f4498190) -------------------------
// ONE shared row builder + POST so the parent pull loop AND the child
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
    // NO gate_state: a producing actor never records a verdict, only what it
    // did. A producer-written 'passed' in the audit spine is self-approval by
    // another route (task 682b7e48). The conductor owns the gate state.
    verdict_summary: (res && (res.validation || res.evidence)) || meta.verdict_summary || null,
    evidence_ref: (res && res.evidence) ? String(res.evidence).slice(0, 200) : null,
  }
  // SANDBOX-REAL TRANSPORT (task 0b34b6f7): the Workflow sandbox cannot do
  // HTTP itself (no fetch / Node net API), so the POST to
  // /api/agent-runs/ingest travels via the STEP AGENT's Bash curl - see
  // telemetryInstr, threaded into every step's contract. Here we journal
  // the canonical row so parent AND child drive paths share ONE shape and the
  // drive log carries the telemetry record alongside the agent's receipt.
  log(`agent-run row (POSTed by the step agent via Bash curl): ${JSON.stringify(row)}`)
  return row
}

// -- Schemas -------------------------------------------------------------
const LOCATE_SCHEMA = {
  type: 'object',
  required: ['task_id', 'title', 'current_step', 'gate_state', 'branch', 'workspace', 'context_summary', 'requirements'],
  properties: {
    task_id: { type: 'string' },
    title: { type: 'string' },
    current_step: { type: 'string', description: 'the step conductor_work handed back; "" means the flow has not been entered' },
    gate_state: { type: 'string', enum: ['none', 'pending', 'passed', 'failed'] },
    branch: { type: 'string', description: 'workspace.branch - the task worktree\'s own branch (never main)' },
    workspace: { type: 'string', description: 'workspace.path from conductor_work: the per-task git worktree every edit/test/commit must use. The gate verifier reads this exact path.' },
    halt_reason: { type: 'string', description: 'set ONLY if the task is blocked or the workspace could not be claimed; empty otherwise' },
    context_summary: { type: 'string', description: 'brain-first summary of the relevant subsystem with file:line refs' },
    requirements: { type: 'array', items: { type: 'string' }, description: 'discrete acceptance requirements distilled from the task' },
    conventions: {
      type: 'array',
      description: 'the importance-ranked, top-N-capped conventions from context_bundle(persona="dev")["conventions"] - push-injected into every step agent\'s preamble (task 0c811636). Each item: {name, importance, summary|description}. Empty array if context_bundle returned none.',
      items: { type: 'object' },
    },
  },
}

// -- Phase: Pre-flight (fail fast) ---------------------------------------
// AC1: assert the run can even succeed BEFORE the drive - turns 7-min-to-fail
// runs into ~5-second fails with one actionable line. Workflow scripts have no
// fs/process access, so the checks run inside the first agent (read-only bash).
const PREFLIGHT_SCHEMA = {
  type: 'object',
  required: ['ok', 'sane_branch', 'datenow_clean', 'daemon_ok', 'deps_present', 'identity_ok', 'tool_profile', 'halt_reason'],
  properties: {
    ok: { type: 'boolean' },
    sane_branch: { type: 'boolean' },
    datenow_clean: { type: 'boolean' },
    daemon_ok: { type: 'boolean' },
    deps_present: { type: 'boolean', description: 'every DONE depends_on task\'s [conductor:<dep8>] commit is reachable from the chosen base OR carried by some branch (git log --all)' },
    identity_ok: { type: 'boolean', description: 'the MCP tools (the `prism_version` line of prism_guide) point at the SAME daemon as the conductor (curl API_BASE/api/version) - false = MCP misrouted to a different/fork daemon' },
    tool_profile: { type: 'string', description: 'the tool_profile query value of the PRISM MCP url in E:/.prism/.mcp.json; must be `drive` (task 9b0f7c4b)' },
    halt_reason: { type: 'string', description: 'ONE actionable remediation line if ok=false; empty otherwise' },
  },
}
// -- Phase: Claim (instant visibility - owner rule 2026-08-14) ------------
// The moment a drive starts working a task, PRISM must show it. The old
// shape spent ~6 minutes (pre-flight + Locate's context sweep) with the row
// still PENDING and the board reading IDLE while real work was happening
// (task ea3f4a62, observed live on the 01ec3894 drive). VISIBILITY PRECEDES
// READING: this tiny mechanical agent flips the row, links the driving
// session and posts the first heartbeat BEFORE pre-flight, before any file
// is read. Non-fatal by design - a failed claim costs only the visibility
// it exists to provide; pre-flight still owns fail-fast.
const CLAIM_SCHEMA = {
  type: 'object',
  required: ['claimed'],
  properties: {
    claimed: { type: 'boolean', description: 'true only if the task row now reads status=in_progress' },
    already_done: { type: 'boolean', description: 'true if the GUARD READ found status=="done" BEFORE any write - no claim was attempted and status was never touched' },
    note: { type: 'string', description: 'one line on anything that failed (best-effort steps included)' },
  },
}
phase('Claim')
if (TASK_ID && !DRY) {
  const claim = await agent(
    `You have PRISM MCP tools (task_list, task_update, task_link_session) served directly on the drive profile - call them directly, no ToolSearch load. Project slug is "prism".\n\nCLAIM TASK ${TASK_ID} FOR THIS DRIVE - a guard read, then three quick actions, NOTHING else: no context, no grep. Speed is the whole point of this step, except for the ONE read below, which is load-bearing.\n0. GUARD READ, FIRST, BEFORE ANY WRITE: task_list(id="${TASK_ID}", fields=["status"]). If status=="done", this task is ALREADY FINISHED - by a prior drive, or by the machine gate adjudicator racing ahead of you. Return already_done=true, claimed=false, and STOP - do not run steps 1-3, do not call task_update at all. This guard exists because task 39244a32 (2026-08-26) had its correctly-set status=done clobbered back to pending by a relaunched drive's Claim step that skipped this check and blindly flipped status=in_progress over a task that had already, legitimately, finished.\n1. task_update(id="${TASK_ID}", status="in_progress"${SID ? `, session_id="${SID}"` : ''}) - the board must leave the pending column THIS SECOND.\n2. task_link_session(task_id="${TASK_ID}"${SID ? `, session_id="${SID}"` : ''}) - tie the driving session to the task.\n3. First heartbeat via Bash curl (127.0.0.1, NEVER localhost): \`curl -s -m5 -X POST '${API_BASE}/api/drive-heartbeat/beat?project=prism' -H 'Content-Type: application/json' -d '{"task_id":"${TASK_ID}","step":"claim","elapsed_s":0,"last_tool":"claim","work_units":1}'\`\nReturn claimed=true only if action 1 succeeded (2 and 3 are best-effort - note a failure in \`note\`, never retry more than once).`,
    { label: 'claim', phase: 'Claim', schema: CLAIM_SCHEMA, model: TIER_MODEL.mechanical })
  if (claim && claim.already_done) {
    log(`Task ${TASK_ID} is already done - the guard read caught it before any write. Exiting cleanly without touching status (this is the fix for the claim-race clobber bug).`)
    return {
      task_id: TASK_ID,
      dry_run: DRY,
      already_done: true,
      done: true,
      started_at_step: 'claim',
      ended_at_step: 'claim',
      halted: null,
      note: 'Guard read found status=="done" before any write; the drive exited immediately without claiming or touching status. Re-check the task directly for its real gate/shipping state - this drive did not re-derive it.',
    }
  }
  if (claim && claim.claimed) {
    log(`Task ${TASK_ID} claimed - the board shows in_progress before anything is read.`)
  } else {
    log(`Claim did not land (${(claim && claim.note) || 'no detail'}) - continuing; pre-flight owns fail-fast and will catch a dead daemon.`)
  }
} else {
  log(TASK_ID ? 'Dry-run: no claim (no mutations).' : 'No task_id supplied - Locate claims the instant task_next names one.')
}

phase('Pre-flight')
const preflight = await agent(
  `${PRISM_TOOLS}\n\nPRE-FLIGHT GUARD - run these READ-ONLY checks from E:/.prism and fail FAST. Use 127.0.0.1, NEVER localhost (gitbash resolves localhost->::1 while the daemon binds IPv4 - a silent-zero trap that has burned whole runs).\n\n1. SANE BRANCH: after \`git -C E:/.prism fetch -q origin main\`, read \`git -C E:/.prism rev-parse --abbrev-ref HEAD\` and \`git -C E:/.prism rev-list --count HEAD..origin/main\`. sane_branch=false if the current branch is BEHIND origin/main by >0 (the stale-branch trap - the drive would build on stale code). main or an even/ahead feature branch is fine.\n2. CLOCK-CLEAN: \`git -C E:/.prism grep -nE 'Date[.]now|new[[:space:]]*Date[(]' -- .claude/workflows/*.js\`. For EACH hit, open that line: if it is a COMMENT (the line, left-stripped of whitespace, starts with \`//\`, \`*\` or \`/*\`) it is ALLOWED - this ban documents itself in comments, and task 3a3f90da (2026-08-26) found a real drive halted twice by a comment plainly explaining the rule, tripping its own grep. datenow_clean=false ONLY on a hit that is REAL CODE, not a comment - this exact scope (comments exempt) is what services/prism-service/tests/unit/test_workflow_scripts_no_datenow.py already enforces; do not be stricter than the pinned test. PRISM is the time authority (it server-stamps run timestamps); a workflow script must never use a client clock in real code (unavailable in the sandbox; breaks resume/cache; PRISM memory mx-9945f2).\n3. DAEMON/CONDUCTOR REACHABLE: \`curl -s -m5 -o /dev/null -w '%{http_code}' ${API_BASE}/api/version\` must be 200 - the conductor cannot record transitions (or stamp time) against a dead daemon.${DRY ? ' (DRY-RUN: treat a dead daemon as non-fatal.)' : ''}\n4. DEPENDENCY PRESENCE: read this task's \`depends_on\` via task_list (the dependencies/depends_on field). For EACH depends_on dep that is DONE, confirm its substrate is present: its [conductor:<dep8>] commit (first 8 chars of the dep id) must be reachable from the chosen base OR carried by some branch - \`git -C E:/.prism log --all --grep "conductor:<dep8>" -n1\` must hit, and that commit must be an ancestor of origin/main OR of a branch returned by \`git -C E:/.prism branch -a --contains <sha>\`. deps_present=false ONLY when a DONE dep's commit is unreachable from the base AND no branch carries it. (No deps, or all dep commits reachable, => deps_present=true.)\n5. MCP-DAEMON IDENTITY MATCH: the reachability check (step 3) proves the CONDUCTOR daemon is up, but NOT that YOUR MCP tools point at that same daemon - a duplicate ~/.claude.json key can bind mcp__prism__* to a DIFFERENT daemon (a fork, or another port) while the conductor HTTP endpoint (${API_BASE}) is the store you think you're driving. Call \`prism_guide\` (MCP, a drive-profile member; the admin-only status verb is NOT on the drive profile, task 9b0f7c4b) and read the \`prism_version\` it reports (mcp_ver); \`curl -s -m5 ${API_BASE}/api/version\` and read that \`version\` (http_ver). identity_ok=false if mcp_ver !== http_ver - your MCP is bound to a different daemon than the conductor and the whole drive would mutate the WRONG store. Also compare \`the admin-only status verb.data_dir\` against the expected store when known.${DRY ? ' (DRY-RUN: a dead daemon can not answer either endpoint; treat identity as non-fatal.)' : ''}\n\n6. DRIVE TOOL PROFILE: \`grep -o 'tool_profile=[a-z_]*' E:/.prism/.mcp.json\` - report the value after \`=\` as tool_profile (empty string if no match). It MUST be \`drive\`: any other value means this lane's MCP surface is the big interactive/all list and the harness defers brain_* behind ToolSearch (task 9b0f7c4b). If tool_profile !== "drive", ok=false with halt_reason "MCP tool_profile=<value>, expected drive - set tool_profile=drive on the prism url in .mcp.json and reconnect".\n\nSet ok=true ONLY if sane_branch AND datenow_clean AND deps_present AND tool_profile === "drive"${DRY ? '' : ' AND daemon_ok AND identity_ok'}. If ok=false, halt_reason = ONE actionable line, e.g. "branch <b> is N behind origin/main - rebase or branch fresh off main", "client clock in <file>:<line> - PRISM server-stamps time, remove it (mx-9945f2)", "conductor daemon down at ${API_BASE} - start it before driving", "MCP daemon <mcp_ver> != conductor daemon <http_ver> - /mcp reconnect prism to the daemon serving ${API_BASE} (or restart the session) before driving", or "dep <id> done but not merged to main and no branch carries it - merge it first".`,
  { label: 'pre-flight', phase: 'Pre-flight', schema: PREFLIGHT_SCHEMA, model: modelFor('', 'pre-flight', false) })
if (!preflight.ok) {
  // HALT VISIBILITY (task ea3f4a62): the Claim phase flipped the row to
  // in_progress, so a silent throw here would leave a driverless in_progress
  // task - the exact dishonest state this workflow exists to prevent. Un-claim
  // and stamp the halt reason onto the drive heartbeat so the task Trace says
  // WHY the drive died instead of showing nothing. Best-effort: if the daemon
  // is the thing that is down, these calls fail quietly and the throw still
  // carries the reason to the invoker.
  //
  // GUARD, SAME BUG AS CLAIM (task 39244a32, 2026-08-26): between Claim
  // setting in_progress and pre-flight halting, the machine gate adjudicator
  // can race in and legitimately finish the task (status -> done). A blind
  // "un-claim to pending" here would then clobber that real completion back
  // to pending, exactly as it did that day. Re-read status FIRST and only
  // un-claim when it is NOT already done.
  if (TASK_ID && !DRY) {
    await agent(
      `You have PRISM MCP tools (task_list, task_update) served directly on the drive profile - call them directly, no ToolSearch load. Project slug "prism". The implement drive for task ${TASK_ID} HALTED at pre-flight: ${preflight.halt_reason || 'a pre-flight check failed'}.\n0. GUARD READ FIRST: task_list(id="${TASK_ID}", fields=["status"]). If status=="done" already, DO NOT touch it - the task finished legitimately (e.g. the machine gate adjudicator) while this drive was starting up. Return claimed=false with note="already done, left untouched" and stop.\n1. Otherwise: task_update(id="${TASK_ID}", status="pending") - the drive is dead, the row must not sit in_progress with no driver.\n2. Bash curl: \`curl -s -m5 -X POST '${API_BASE}/api/drive-heartbeat/beat?project=prism' -H 'Content-Type: application/json' --data-binary '{"task_id":"${TASK_ID}","step":"pre-flight","elapsed_s":60,"last_tool":"pre-flight-halt: ${String(preflight.halt_reason || 'check failed').replace(/["\\]/g, '')}","work_units":2}'\`\nReturn claimed=false with a note either way. Best-effort - a failure here is non-fatal.`,
      { label: 'halt-visible', phase: 'Pre-flight', schema: CLAIM_SCHEMA, model: TIER_MODEL.mechanical })
  }
  throw new Error(`PRE-FLIGHT HALT - ${preflight.halt_reason || 'a pre-flight check failed'} [sane_branch=${preflight.sane_branch} clock_clean=${preflight.datenow_clean} daemon_ok=${preflight.daemon_ok} deps_present=${preflight.deps_present} identity_ok=${preflight.identity_ok} tool_profile=${preflight.tool_profile}]`)
}
log(`Pre-flight OK - branch sane, workflow scripts clock-clean (PRISM owns time)${DRY ? '' : ', daemon reachable'}.`)

// -- Phase: Locate -------------------------------------------------------
phase('Locate')
const pick = TASK_ID
  ? `Read the driven task via task_list(id="${TASK_ID}") - a LEAN by-id read that returns JUST this one task (a 1-element list), NOT the whole board (a full board is ~100x the tokens).`
  : 'No task id was supplied. Call task_next to choose the highest-priority unblocked task; it returns that single task (do NOT pull the whole board).'

// VISIBILITY PRECEDES READING (owner rule 2026-08-14, task ea3f4a62): the
// claim + first heartbeat are Locate's FIRST tool calls, before any brain or
// grep work - the context sweep runs minutes, and an unclaimed sweep renders
// as "nothing is being done" on the owner's board. Idempotent on the
// task_id path (the Claim phase already flipped the row); load-bearing on the
// server-pull path where the task id is only known after task_next.
const sidArg = SID ? `, session_id="${SID}"` : ''
const claimFirstInstr = DRY ? '' : [
  `- CLAIM VISIBILITY FIRST - your first tool calls after reading the task row, BEFORE any brain/grep/context work: task_update(id="<the task id>", status="in_progress"${sidArg}) then task_link_session(task_id="<the task id>"${sidArg}). If the Claim phase already flipped the row these are idempotent no-ops - run them anyway; on the task_next path they are the ONLY claim.`,
  `- FIRST HEARTBEAT immediately after the claim, then re-beat every 5-8 tool calls THROUGHOUT the context sweep below: \`curl -s -m5 -X POST '${API_BASE}/api/drive-heartbeat/beat?project=prism' -H 'Content-Type: application/json' -d '{"task_id":"<the task id>","step":"locate","elapsed_s":<seconds since you started>,"last_tool":"<the tool you just ran>","work_units":<counter that strictly increases>}'\` - without beats the owner's board reads a healthy sweep as nothing happening.`,
  '',
].join('\n')

const locate = await agent(
  `${preamble('analyst')}\n\nGOAL: locate the task and orient before the conductor drive.\n\n${pick}\n\nThen, IN THIS ORDER - visibility precedes reading:\n${claimFirstInstr}- Report current_step (workflow_step) and gate_state exactly as stored.\n- Build a brain-first context_summary of the subsystem this task touches (brain_search/brain_understand first; disk grep - the Grep tool OR Bash-shelled grep/rg/sed/find - only for gaps the Brain does not answer), with file:line refs.\n- Distill the task description + any acceptance criteria into a discrete \`requirements\` list - each item independently testable.\n- PUSH-INJECT CONVENTIONS (task 0c811636): call \`context_bundle(persona="dev")\` ONCE and return its \`conventions\` array verbatim as the \`conventions\` field - this is PRISM's importance-ranked, top-N-capped living feedback doctrine (the domain="feedback" conventions - render policy, gate enforcement, board hygiene, etc.) that the drive injects into EVERY subsequent step agent's preamble. If the tool result OVERFLOWS the harness cap ("exceeds maximum allowed tokens ... saved to <path>.txt" - observed at 1.5 MB on drive wf_c7bae879, task 3a3f90da AC-2), do NOT return \`[]\`: read the saved file and extract the \`conventions\` array from it (\`grep -o '"conventions": *\\[' -A 200 <path>\` or Read with offset). If the bundle truly has no \`conventions\` key or it is empty, return \`[]\` (the drive falls back to the static procedural spine + memory_recall self-heal).\n- CLAIM THE TASK WORKTREE (this REPLACES cutting a feature branch in the shared checkout). ${DRY ? 'Report the workspace path you WOULD claim (data_dir/task_workspaces/<task_id>) without calling conductor_work.' : 'Call `conductor_work(id="<the task id>")` with NO outcome - that is the idempotent START/PEEK: it enters the flow, runs task_workspace.ensure_workspace(), and returns `workspace` {path, branch, baseline, repo_root} together with the first self-describing `job`. Report workspace.path as `workspace`, workspace.branch as `branch`, the job\'s `step` as current_step, and its gate_state.'} EVERY later edit, test run and commit for this task happens with \`git -C <workspace.path>\` or cwd=<workspace.path> - the gate verifier reads exactly that path, and work left in the shared E:/.prism checkout is invisible to it. A conductor_work ok:false carrying a workspace error is a HARD stop (it fails closed on purpose so two tasks can never share a branch): put it in halt_reason and do not proceed.\n- BRANCH: BASE SAFETY. The server cuts the worktree for you, but \`ensure_workspace\` defaults its base_ref to the repo's CURRENT HEAD - NOT origin/main - so a stale shared checkout silently yields a stale worktree, and the slice would be built on code that is behind. Verify it rather than assume: \`git -C "<workspace.path>" fetch -q origin main && git -C "<workspace.path>" rev-list --count HEAD..origin/main\` MUST return 0. If the worktree base is BEHIND origin/main, STOP and say so in halt_reason (name the count) - do not drive a slice onto a stale base.${DRY ? ' In DRY-RUN, report the base you WOULD verify and the depends_on you WOULD check, without claiming a workspace.' : ''}\n- DEPENDENCY CHECK (do NOT pick a base - the server already cut the worktree): read this task's \`depends_on\`. For each dep that is DONE, confirm its \`[task:<dep8>]\` / \`[conductor:<dep8>]\` commit is reachable from the worktree baseline (\`git -C <workspace.path> log --grep "<dep8>" -n1\`, falling back to \`git -C E:/.prism log --all --grep "<dep8>" -n1\`). A dep that is NOT done means this task is BLOCKED: report that in halt_reason and do not drive.\n\nReturn the structured locate result.`,
  { label: 'locate', phase: 'Locate', schema: LOCATE_SCHEMA, model: TIER_MODEL.balanced })

if (locate.halt_reason && String(locate.halt_reason).trim()) {
  throw new Error(`LOCATE HALT - ${locate.halt_reason}`)
}
const WS = String(locate.workspace || '').trim()
if (!DRY && !WS) {
  throw new Error('LOCATE HALT - no task workspace was returned by conductor_work. '
    + 'The flow fails closed rather than sharing a branch; nothing can be driven safely.')
}
log(`Task "${locate.title}" is at "${locate.current_step || '(not entered)'}" (gate=${locate.gate_state}).`)
log(`Task worktree: ${WS || '(dry-run)'} on branch ${locate.branch}. The gate verifier reads THIS path - all work lands here.`)

// Seed the push-injected live conventions from context_bundle (task 0c811636).
// From here on EVERY step handler's preamble() carries the importance-ranked,
// top-N-capped feedback doctrine - replacing the old frozen CONVENTIONS array.
// memory_recall (SELF_HEAL) stays the fallback, not the primary source.
setLiveConventions(locate.conventions)
const _convCount = Array.isArray(locate.conventions) ? locate.conventions.length : 0
log(`Push-injected ${_convCount} live convention(s) from context_bundle into every step preamble.`)

// -- Per-step handler prompts --------------------------------------------
const ctx = `TASK: ${locate.title} (id ${locate.task_id})\nBRANCH: ${locate.branch}\nREQUIREMENTS:\n- ${locate.requirements.join('\n- ')}\n\nCONTEXT (brain-first):\n${locate.context_summary}`

// The workspace rule, restated in EVERY worker prompt. This is the single
// highest-leverage line in the file: it is what makes the gate verifier able
// to see the work, which is what makes override=true unnecessary.
const WORKSPACE_RULE = DRY ? '' : [
  '',
  `WORKSPACE (absolute): this task's worktree is \`${WS}\`.`,
  `- Run EVERY command with \`git -C "${WS}" ...\` or with cwd="${WS}". Read and edit files under "${WS}", NOT under E:/.prism.`,
  '- The gate verifier resolves this task\'s evidence from that exact path (api/conductor_flow.py:262). Anything you change in the shared checkout is invisible to the gate and will strand it.',
  '- The shared checkout also holds other sessions\' uncommitted work. Never `git add -A`, never `git checkout -- <file>`, never commit from E:/.prism on this drive.',
  `- Commit with the task trailer: \`git -C "${WS}" add <the files you touched> && git -C "${WS}" rev-parse --abbrev-ref HEAD && git -C "${WS}" commit -m "<type>(<scope>): <one-line> [task:${(locate.task_id || '').slice(0, 8)}]"\` (branch re-read in the SAME command - this tree is shared).`,
].join('\n')

// How a worker hands its step back. ONE verb: conductor_work. The server
// advances (or decides), then returns the NEXT job - the worker never names
// the next step and never calls conductor_advance/conductor_gate.
function reportInstr(job) {
  const step = (job && job.step) || 'the current step'
  return [
    '',
    'REPORT (the ONLY way to hand this step back):',
    `  conductor_work(id="${locate.task_id}", outcome="pass", proof=<the artifact named by expected_proof>, model="<the model you ran on>")`,
    `- \`proof\` is not a summary, it is THE ARTIFACT. The server routes it by step: draft_story and verify_plan write it to task.plan_doc (the story/plan RUBRICS read plan_doc, so pass the FULL story/plan markdown, never a note about it); review_previous_notes writes task.premise_notes; every other step writes task.completion_proof, which is what the green_gate artifact tooth reads.`,
    `- Report a FAILURE honestly: outcome="fail" records the failure and does NOT advance (the step stays put). A reported outcome is not step completion - do not report "pass" on work you did not finish.`,
    `- The call returns {ok, done, job, detail}. \`job\` is the NEXT self-describing job. Report that job's step/kind/role/gate_state back to the drive as next_step/next_kind/next_role/gate_state so the loop can route it.`,
    `- Do NOT pass \`fields\` to conductor_work (it has no such parameter - id/outcome/proof/override/model only), and do NOT call conductor_advance, conductor_gate or workflow_state: they are superseded admin verbs and hand-driving them desyncs the flow.`,
    `- If the response says the report was stale/noop ("expected_step does not match"), the conductor is simply AHEAD of you: that is NOT a failure. Set ok:true and report the ACTUAL current step from the returned job so the loop continues.`,
    telemetryInstr(step, (job && job.role) || 'agent'),
  ].join('\n')
}

// -- STEP-SPECIFIC AMPLIFIERS -------------------------------------------
// The server's job.instructions are deliberately terse (api/conductor_flow.py
// _GUIDE). These add the hard-won specifics the bare instruction cannot carry:
// the graph rungs that set the real blast radius, and the two authoring
// contracts whose rubrics have actually stranded live drives.
const STEP_EXTRA = {
  review_previous_notes: [
    'GRAPH RUNG (WHY first): memory_recall + brain_search for prior decisions, failures and conventions on this surface. Name the gotchas a builder must respect, each with a file:line.',
    'The proof for this step lands in task.premise_notes - write the grounded premise, not a summary of your reading.',
    'RUBRIC CONTRACT (premise_grounded): every load-bearing claim under `## Premises` needs EITHER a citation (file:line, a run/PR/commit/issue id, or backtick command output) OR an explicit REFUTED/UNVERIFIED marker - never neither. A citation written as its OWN nested child bullet under the claim (more indented than the claim\'s own bullet) folds into that claim; a citation bullet at the SAME indent as the claim starts an untracked entry of its own and the claim it should have grounded reads as bare and ungrounded. Same indentation rule as draft_story\'s oracle contract above - when in doubt, put the citation on the SAME line as the claim instead of a separate bullet, which is unambiguous either way.',
  ].join('\n'),
  draft_story: [
    'RUBRIC CONTRACT (this exact shape, or story_gate parks): the proof you send IS task.plan_doc. It must carry `## Summary`, `## Requirements` (FR-<n>/NFR-<n> ids), and `## Acceptance Criteria` where every criterion is `AC-<n>` (the hyphen is required - `AC1` does not match) and ends with a line `- oracle: <observable check>`.',
    'THE ORACLE LINE MUST BE A CHILD BULLET, MORE INDENTED THAN ITS AC, NEVER A SAME-LINE SUFFIX - the rubric parser folds a nested line into its AC by comparing indentation; a line indented the SAME as (or less than) the AC becomes a new, untracked entry of its own and the AC it should have belonged to reads as oracle-less. Correct shape (2 leading spaces shown, any consistent deeper indent works):\n  - AC-1: <criterion, one line>\n    - oracle: <the observable check>\n  - AC-2: <criterion, one line>\n    - oracle: <the observable check>\nWRONG shapes that WILL strand story_gate: the oracle bullet at the SAME indent as its AC (reads as a sibling AC-less entry); the oracle appended after the AC text on one line with no bullet of its own (also fine, but only if it is genuinely on the SAME physical line - do not split it onto its own line at the same indent, which is the shape that actually broke task 3a3f90da on 2026-08-26, all 9 ACs, before arc_governance.py\'s _ac_lines and PlanView.tsx\'s parseAcLines were both fixed the same day).',
    'SELF-CHECK BEFORE YOU REPORT: re-read your own drafted `## Acceptance Criteria` section line by line. For every `- oracle:` line, confirm the line above it in the raw text is indented STRICTLY LESS than the oracle line (count the leading spaces yourself, do not eyeball it) - if it is not, fix the indentation before sending `proof=`. This is the exact defect class that stalled a live drive for the length of a full gate-wait window with a story that read correctly to a human but not to the rubric.',
    'Pass the FULL story markdown as `proof=`. A task_update(plan_doc=...) you make beforehand is CLOBBERED by this very report.',
    'ORACLE: set the observable signal that proves the user outcome via task_update(oracle=..., proof_type=...). RESPECT a proof_type the task already declares (metric/artifact/demo/review) - do NOT clobber it to "test". Only default proof_type="test" when the task has none.',
    'AN ORACLE THAT EVERY COLLABORATOR CAN MOCK IS NOT AN ORACLE: it must name a USER-REACHABLE surface (the route a click lands on, the MCP verb through the dispatcher, the API field), not a class that can be constructed in a test.',
  ].join('\n'),
  verify_plan: [
    'RUBRIC CONTRACT (this exact shape, or plan_gate parks): the proof you send IS task.plan_doc - it must cover EVERY AC id from the story. Separately, plan_gate reads the plan_diagram FIELD, not a ```mermaid fence inside plan_doc: persist it with task_update(plan_diagram=<mermaid source>) (consult the mermaid-syntax skill; first line a bare diagram keyword). An empty architecture-principle store never passes - if the rubric says principles are unseeded, seed them once per services/arc_governance.py.',
    'GRAPH RUNG (blast radius, and this is what makes green honest): for each symbol the plan will change, run brain_call_chain(entity=<symbol>, direction="callers", depth=2) and brain_find_references(name=<symbol>). The callers you find are the BLAST RADIUS. Two consequences, both mandatory: (1) allowed_files must cover what you will actually touch, and (2) every NEIGHBOURING suite that pins a caller must be listed and run at verify_green - task.verify is the gate\'s evidence, NEVER the blast radius. Two live tasks went machine-green on their own file while main sat red with three contradictory tests.',
    'If the plan DELETES or CHANGES a contract an existing test pins, retire those assertions IN THIS SLICE with a comment naming what superseded them. A contradiction left standing is a red main.',
    'CHECK control_plane.POLICY_FILES: if the slice would touch a gate-policy file, it will fail its own gates on the candidate-controls-judge tooth. Split the consumer into a non-policy file and say so in the plan.',
    'PROACTIVE RESEARCH RUNG (size-gated, grounding-gated): classify each non-trivial approach in the plan. If an approach is UNGROUNDED in BOTH Brain and grep - neither brain_search/memory_recall (the WHY) nor a disk Grep/Read of existing source - including Bash-shelled grep/rg/sed/find, which count as the same disk retrieval (the HOW) can ground the technique, i.e. it is a NEW practice not yet present in this codebase - then this step BLOCKS. Do NOT pass verify_plan until a cited WebSearch / best-practice pass exists for it: set ok:false and name the ungrounded approach in halt_reason UNLESS you have run a WebSearch and can cite the source (url/title) that validates the practice. SIZE GATE: a trivial, one-line, or pattern-already-in-repo approach is grounded by grep and needs NO web rung - only an ungrounded, non-trivial NEW practice triggers the blocking research requirement. Set source_tier="web" only when this rung actually fired.',
  ].join('\n'),
  write_failing_tests: [
    'Write the SMALLEST set of tests that pin the acceptance criteria and FAIL today, in the task worktree.',
    'ASSERT THE AFFORDANCE A PERSON USES, never the constant behind it. A test that imports a service class or checks an enum member passes even when the code is DEAD (no route, no nav entry, no dispatcher entry, no rendered element). For UI, pin the entry point a human actually clicks; if that file is outside allowed_files the slice is mis-scoped - say so rather than testing the reachable proxy.',
    'For source-reading tests, match the RENDERED TAG (e.g. `<ClaudeAuthCard`), never the bare name, and parse the enclosing JSX branch - a comment above the element has satisfied this kind of assertion three separate times.',
    'COMMIT THE TESTS ALONE, before any implementation, as a TESTS-ONLY commit carrying the [task:<id8>] trailer. The red machine seat anchors to that commit; bundling tests with implementation makes red undemonstrable and strands red_gate on a human.',
    'Run the exact test command in the worktree and capture the FAILING trace - it must fail for the RIGHT reason. That trace is the proof.',
    'Set task.verify to the workspace-root-relative test path (e.g. `services/prism-service/tests/unit/test_x.py`, never `cd services/prism-service && pytest ...`) - a non-relative verify makes the red check exit 4 "no tests ran" instead of the rc==1 it needs.',
  ].join('\n'),
  implement_tasks: [
    'Make the SMALLEST change that turns the failing tests green. Chunk edits to ~30 lines. Reuse existing patterns (brain-first).',
    'HARD SCOPE: re-read the job\'s contract.allowed_files. The server checks it against the REAL workspace diff when you report - a file outside it refuses the report. If you genuinely need one, STOP and say so in halt_reason rather than pushing through (that is what contract.stop_if is for).',
    'Respect the Brain-stored architecture principles (memory_recall "architecture principles layer rules") - green_gate diffs the observed layer edges against them.',
    'If the change is user-visible, patch-bump PRISM_VERSION in the same commit.',
  ].join('\n'),
  verify_green_state: [
    'Run the FULL relevant suite in the worktree, not just the new tests - INCLUDING every neighbouring suite the plan\'s call-chain rung named. Report anything red as outcome="fail"; a red test is always yours, never "pre-existing".',
    'TESTS-PASS IS NOT FEATURE-WORKS. If the ticket names a user-reachable surface, exercise it for real (curl the running daemon / drive the page) and LOOK at the result before claiming it.',
    'EVIDENCE GOES INTO PRISM, never an external host: write screenshots/artifacts to <data_dir>/evidence/<task_id>/ and cite them in the proof as `![](/api/tasks/<task_id>/evidence/<file>)`. Read <data_dir> from prism_guide (or curl API_BASE/api/version).',
    'Reporting SUCCESS here is what MINTS the green EvidenceReceipt (svc.mint_green_evidence runs on this report, from the task worktree). That receipt is what the green_gate seat decides on - so the quality of this step is the quality of the whole gate.',
  ].join('\n'),
  green_gate: [
    'MANDATORY WHY-CAPTURE ON SUCCESS: a clean terminal pass is a DECISION, and the WHY must be written back to the source of truth, not just on failure (the SELF-HEAL ladder covers only failures). Once this gate is decided green, call memory_store(type="decision", ...) carrying the full WHY contract: the DECISION made, its RATIONALE, the REJECTED ALTERNATIVES (what you did NOT do, and why), and concrete file:line refs to the change. memory_recall must surface this decision memory after a clean drive - a terminal success that records completion_proof but no decision memory has NOT written the WHY back.',
    'This is a WRITE-BACK, not a gate action: it records why the change is what it is. It never approves anything, and it must not be skipped just because the machine seat decided the gate for you.',
  ].join('\n'),
}

// -- GATE SEAT: make the evidence real, then WAIT ------------------------
// This drive does NOT decide gates. flow_report refuses a gate report from a
// session that produced any prior step, and that refusal is the point: a
// producing actor clearing its own gate is the failure this whole system
// exists to prevent. So the gate handler's job is narrow and honest:
//   1. read the LIVE readiness (never the stored gate_reason - it is a
//      snapshot that has misled a driver for three turns straight),
//   2. if a tooth is unsatisfied, FIX THE EVIDENCE (not the rubric),
//   3. poll until a DISTINCT seat decides, or report exactly what the human
//      must click and why.
function gatePrompt(job) {
  const tid = locate.task_id
  const step = job.step
  return [
    `STEP ${step} - BLOCKING GATE. You are NOT the decider.`,
    '',
    'THE RULE: the producing session cannot clear its own gate. Do NOT call conductor_gate. Do NOT report a gate outcome through conductor_work. Do NOT pass override. Your job is to make the evidence real and let a distinct seat decide.',
    '',
    `1. READ THE LIVE TRUTH (not task.gate_reason - that is a stored snapshot):`,
    `   curl -s -m10 "${API_BASE}/api/conductor/gate/readiness?task_id=${tid}&project=prism"`,
    `   It returns what a plain Approve would actually consult: receipt_ok, the refusal reason, adapter, and whether it needs manual review. Quote it verbatim in your evidence.`,
    '',
    '2. ACT ON WHAT IT SAYS:',
    `   - story_gate / plan_gate park on a RUBRIC verdict. The reason names the exact gap (a missing \`## Section\`, an \`AC1\` that should be \`AC-1\`, a plan_diagram absent from the FIELD, unseeded principles). FIX THE TASK (task_update plan_doc / plan_diagram), never the rubric. The ~20s adjudicator sweep re-scores it; you do not need to report anything.`,
    `   - red_gate wants rc==1 from the pinned suite at the tests-only commit. "no tests ran" (rc=4) means task.verify is not workspace-root-relative - fix task.verify. Evidence not on file means the tests-only [task:<id8>] commit is missing from this worktree - commit it.`,
    `   - green_gate wants a FRESH PASSING EvidenceReceipt minted from this worktree. If the receipt is stale or absent, re-run the oracle: curl -s -m60 -X POST "${API_BASE}/api/conductor/gate/mint" -H 'Content-Type: application/json' --data-binary '{"task_id":"${tid}"}'  (long JSON bodies with unicode must go through --data-binary @file, never inline -d).`,
    '',
    `3. WAIT FOR THE SEAT (bounded, ${GATE_WAIT_S}s): poll every 15s -`,
    `   curl -s -m10 "${API_BASE}/api/conductor/gate/readiness?task_id=${tid}&project=prism"  and  conductor_work(id="${tid}")  (no outcome - a pure PEEK).`,
    `   The moment the peek returns a job whose step is PAST this gate (or done:true), the gate was decided: report ok:true with the new step. In this environment the machine adjudicator seat runs on a ~20s interval, so a genuinely-evidenced gate usually clears inside a minute.`,
    '',
    (isHumanGate(step)
      ? `4. IF IT DOES NOT CLEAR: ${step} IS one of the owner's two gates, so a human decision is legitimate here. Set ok:false and say plainly what the owner reviews and where. Two cases deserve plain language:`
      : [
        `4. IF IT DOES NOT CLEAR: STOP. ${step} is a MACHINE gate. The owner stops ONLY at plan_gate and green_gate (owner rule mx-1eb0a9). A machine seat that will not decide is a DEFECT to diagnose, never a click to request - do NOT write "the owner must approve ${step}" in halt_reason. Diagnose in this order and report the CAUSE:`,
        `   (a) STALE WORKSPACE BASELINE - the known false positive. control_plane.candidate_policy_edits diffs the worktree against workspace.baseline, a SNAPSHOT that goes stale the moment main moves. A POLICY_FILES entry that arrived from an already-merged FOREIGN commit is then attributed to this candidate and the seat abstains silently every sweep, forever. CHECK IT: compare the stored baseline in services/prism-service/data/task_workspaces/index.json against \`git -C <workspace> merge-base HEAD origin/main\`. If they differ, that is the cause: report it, name both shas, and say the judge should diff the merge-base (or exclude commits already ancestors of origin/main). Do NOT tag the task 'policy-change', do NOT set PRISM_POLICY_CHANGE_APPROVED=1, and do NOT rewrite the baseline yourself - a candidate editing the judge's own diff input games the distinct-actor rule.`,
        `   (b) THE REASON MAY BE A CURED SNAPSHOT. task.gate_reason is written once and not refreshed, so it can describe a gap the drive already fixed. Before repeating it, run the real scorer against the LIVE artifact (arc_governance.score_story_complete / score_plan_coverage over task.plan_doc). If it now passes, say so and name the true blocker instead.`,
        `   (c) Only once (a) and (b) come back clean, report the unsatisfied tooth verbatim.`,
      ].join('\n')),
    `   - green_gate on proof_type=demo or review is HUMAN-ONLY by owner rule (adjudicate_green_gate returns None for them deliberately, because a machine seat once false-greened two tasks and ate the single sign-off). Do NOT add a loadable URL to the oracle, do NOT flip proof_type, do NOT reach for override - that games the distinct-actor rule. Instead make the click GROUNDED: a live demo URL, durable screenshots in the PRISM evidence store, and a green machine-checkable verify. Then say: the evidence is ready, the owner approves at ${API_BASE}/tasks/${tid}?project=prism (cite the task as a linked title, never a bare hex id).`,
    `   - a readiness saying receipt_ok:true with "your review is the sign-off" means the Approve is ALREADY clean and enabled. Say that, rather than telling a human they are blocked.`,
    '',
    'NEVER report a gate as machine-decided without reading its receipt first. A pass is a claim about evidence: open the evidence, quote the adapter and the tree sha, and confirm the tree is THIS task\'s.',
    '',
    // Gate agents do NOT run through preamble()/SELF_HEAL (this prompt is
    // built standalone, above), so the environment-collision signature is
    // injected directly here - a gate agent's Bash calls (readiness curl,
    // gate/mint curl) are exactly as exposed to the harness cwd-isolation
    // leak as any worker step's.
    ENV_COLLISION_DOCTRINE,
    (STEP_EXTRA[step] ? `\nSTEP SPECIFICS:\n${STEP_EXTRA[step]}` : ''),
    telemetryInstr(step, 'gate'),
  ].join('\n')
}

// -- WORKER SEAT: do exactly the job the server handed back ---------------
// One agent per job. It receives the server's OWN instructions/expected_proof
// verbatim (never a step name this script invented), the amplifier for that
// step, the worker contract, and the workspace rule.
function workerPrompt(job) {
  const c = (job && job.contract) || {}
  const allowed = (c.allowed_files || []).join(', ')
  const verify = (c.verify || []).join(' ; ')
  const stopIf = (c.stop_if || []).join(' ; ')
  const extra = STEP_EXTRA[job.step] || ''
  // THE PARENT STOPS BUILDING once children exist (epic 2d480b08). Its job
  // becomes the one thing no child can do: prove the ASSEMBLED product works
  // through the real wiring, which is also the epic's own oracle receipt.
  const assembleOnly = childrenOwnTheBuild ? [
    '',
    'CHILDREN OWN THE IMPLEMENTATION. This task was decomposed and its slices have already been driven in their own worktrees.',
    'DO NOT re-implement anything a child owns. If you find yourself writing the feature, you are duplicating a slice that is already green, and two implementations of one seam cannot both be merged.',
    'Your work here is ASSEMBLY AND PROOF: integrate what the children shipped, verify the whole thing end-to-end through the REAL production path (not an injected collaborator), run every neighbouring suite, and fill only genuine gaps NO child covered - naming the gap explicitly in your evidence.',
    'If the assembled product already satisfies this task\'s oracle with no new code, say exactly that and report the step with the assembly evidence. Writing nothing is the correct outcome when the children already delivered.',
  ] : []
  return [
    preamble(job.role || 'dev'),
    ...assembleOnly,
    '',
    `STEP ${job.step} (role ${job.role || '-'} / ${job.role_label || '-'}). The SERVER chose this step - you never pick one.`,
    '',
    `FIRST, PEEK YOUR OWN JOB (authoritative): call \`conductor_work(id="${locate.task_id}")\` with NO outcome. It is a read-only peek that returns {job:{step, kind, role, instructions, expected_proof, gate_reason, doctrine, contract}}. TRUST THAT RESPONSE over anything below - if it names a different step, the conductor moved and you work THAT step instead (say so in your evidence).`,
    '',
    'THE SERVER\'S OWN INSTRUCTIONS FOR THIS JOB (do EXACTLY this):',
    job.instructions || '(peek to read them - the drive did not have them cached)',
    '',
    `EXPECTED PROOF: ${job.expected_proof || 'n/a'} - this names the validation the step is scored on. Produce THAT artifact and send it as \`proof\`.`,
    (job.gate_reason ? `\nSERVER-REPORTED REASON ON THIS TASK: ${job.gate_reason}` : ''),
    '',
    (allowed ? `WORKER CONTRACT - allowed_files (HARD scope, checked server-side against the real workspace diff when you report): ${allowed}` : 'WORKER CONTRACT - allowed_files: (unconstrained)'),
    (verify ? `WORKER CONTRACT - verify (the command that proves this slice): ${verify}` : ''),
    (stopIf ? `WORKER CONTRACT - stop_if (HALT, do not push through): ${stopIf}` : ''),
    '',
    `STEP BUDGET - ${stepBudgetFor(job.step)}s (task dcbd284f). This drive script has no clock, so YOU enforce this deadline: note the time at your FIRST command and track elapsed as you go. A drive once ran 3.9 HOURS, ~2.5 of them inside a single step whose subprocess had wedged, because nothing told it to stop.`,
    `- If a command you started is still running as the deadline passes, KILL it and any child process you spawned, then STOP. Do not relaunch it a third time hoping it clears.`,
    `- Then return ok:false with budget_exceeded:true, elapsed_s set to the seconds you actually ran, and step_retries set to how many times you re-ran a command inside this step. Put in halt_reason what you were waiting on and what you did produce.`,
    `- Work you already COMMITTED is kept and reported, so stopping at the deadline never throws away green work. Report the commit shas in evidence.`,
    `- Finishing early is always fine. This is a ceiling, not a target: never pad a step to fill it.`,
    heartbeatInstr(job),
    '',
    ctx,
    GRAPH_BRIEF,
    WORKSPACE_RULE,
    (extra ? `\nSTEP SPECIFICS (hard-won - the bare instruction above cannot carry these):\n${extra}` : ''),
    reportInstr(job),
  ].filter(Boolean).join('\n')
}

// -- Schemas for the pull loop -------------------------------------------
// What a worker/gate agent must hand back so the loop can route the NEXT job
// without the script ever naming a step.
const JOB_RESULT_SCHEMA = {
  type: 'object',
  required: ['step', 'ok', 'next_step', 'gate_state', 'evidence'],
  properties: {
    step: { type: 'string', description: 'the step you were handed' },
    ok: { type: 'boolean', description: 'true only if the work succeeded AND the server accepted the report (or the conductor was merely ahead)' },
    next_step: { type: 'string', description: 'job.step from the conductor_work response - the step the server now wants. Empty string when done.' },
    next_kind: { type: 'string', enum: ['agent', 'gate', ''], description: 'job.kind from the response' },
    next_role: { type: 'string', description: 'job.role from the response' },
    gate_state: { type: 'string', description: 'gate_state from the response (none|pending|passed|failed)' },
    done: { type: 'boolean', description: 'true when conductor_work returned done:true (terminal green_gate passed)' },
    proof_sent: { type: 'string', description: 'the artifact you actually sent as proof= (first ~400 chars) - so the drive log records what the gate will score' },
    evidence: { type: 'string', description: 'what you did, with file:line and the exact command + result' },
    source_tier: { type: 'string', enum: ['brain', 'grep', 'web'], description: 'which tier answered this step: brain=WHY, grep=HOW, web=NEW-PRACTICE (cited). A step citing nothing, or the wrong tier, is a mis-tiered cite - reject it.' },
    retrieval_ledger: { type: 'object', description: 'task 3a3f90da FR-4: the counts this step ACTUALLY ran. disk_calls = Grep tool calls PLUS Bash calls that shell out to grep/rg/ag/sed/awk/find/cat/head/tail.', properties: { brain_calls: { type: 'number' }, disk_calls: { type: 'number' }, first_retrieval_tool: { type: 'string', description: 'name of the FIRST retrieval tool this step called (brain_search, Bash, Read, ...)' } } },
    halt_reason: { type: 'string', description: 'set ONLY on a genuine failure or an undecided gate; name the unsatisfied tooth and what a human must do' },
    budget_exceeded: { type: 'boolean', description: 'true ONLY when you stopped because this step passed its wall-clock budget (task dcbd284f). The drive turns this into a budget halt that KEEPS your committed work, rather than a failure.' },
    elapsed_s: { type: 'number', description: 'seconds this step actually ran, measured by you from your first command. Required to be honest when budget_exceeded is true: "over budget" without a duration cannot tell a slow step from a wedged one.' },
    step_retries: { type: 'number', description: 'how many times you re-ran a command INSIDE this step (e.g. killed a wedged subprocess and relaunched it). The 3.9h incident retried twice and the drive had no idea.' },
  },
}

// -- Phase: Graph (blast radius before a single line is planned) ----------
// The call graph, not intuition, decides what this slice may touch and which
// OTHER suites must stay green. This is the structural answer to the two
// recorded false-green modes: a slice that went green on its own file while
// main sat red on three contradictory tests, and a slice whose oracle was
// satisfiable with every collaborator mocked.
const GRAPH_SCHEMA = {
  type: 'object',
  required: ['entities', 'blast_radius_files', 'neighbouring_suites', 'user_reachable_surface', 'slice_count', 'rationale'],
  properties: {
    entities: { type: 'array', items: { type: 'string' }, description: 'the symbols this task changes, as resolved in the graph' },
    blast_radius_files: { type: 'array', items: { type: 'string' }, description: 'files containing callers/references of those symbols - the candidate allowed_files' },
    neighbouring_suites: { type: 'array', items: { type: 'string' }, description: 'existing test files that pin any caller in the blast radius. These MUST be run at verify_green; they are not optional.' },
    user_reachable_surface: { type: 'string', description: 'the concrete affordance a person touches for this change (route, nav entry, MCP verb through the dispatcher, API field). "none found" is a real answer and means the slice may be mis-scoped.' },
    contradicted_contracts: { type: 'array', items: { type: 'string' }, description: 'assertions in existing tests that this change would CONTRADICT (file:line + what it pins). These must be retired in this slice.' },
    slice_count: { type: 'integer', description: '1 if this is a single coherent slice; >1 if the blast radius spans disjoint surfaces that need their own oracles' },
    rationale: { type: 'string' },
  },
}

phase('Graph')
let GRAPH_BRIEF = ''
const graph = await agent(
  [
    preamble('analyst'),
    '',
    'GOAL: use the CODE GRAPH to fix this slice\'s blast radius before anything is planned. Graph first, disk only for what the graph cannot answer.',
    '',
    ctx,
    '',
    'DO THIS:',
    '1. Resolve the symbols this task changes: brain_find_symbol / brain_search on the task\'s nouns. Report them as `entities`.',
    '2. For EACH entity: brain_call_chain(entity=<sym>, direction="callers", depth=2) answers "who breaks if I change this?", and brain_find_references(name=<sym>) gives the concrete call sites. Union of their files = `blast_radius_files`.',
    '3. Find the tests that pin those callers (brain_search for the caller names in test files, then Grep or Bash-shelled grep/rg to confirm - both count as disk retrieval). Those are `neighbouring_suites` - they MUST run at verify_green. The pinned suite in task.verify is the GATE\'S EVIDENCE, never the blast radius; two slices went machine-green on their own file while main sat red with three contradictory tests.',
    '4. Name the `user_reachable_surface`: the affordance a PERSON touches for this change - the route a click lands on, the nav entry, the MCP verb as reached through the tool dispatcher, the API field on the wire. Assert-the-affordance, never the constant behind it: a section id and its metadata can all be correct while the section stays unreachable because the nav a human clicks lives in another file. If you cannot find one, say "none found" - that is a real answer meaning the slice may be mis-scoped, and the plan must confront it.',
    '5. List `contradicted_contracts`: existing assertions this change would break BY DESIGN (file:line + what it pins). These get retired in this slice with a comment naming what superseded them - a contradiction left standing is a red main.',
    '6. Set `slice_count`: 1 when this is one coherent slice with one oracle. >1 ONLY when the blast radius spans genuinely disjoint surfaces that each need their own oracle and their own allowlist (e.g. a store, an adapter, and a UI surface). Be conservative - splitting has its own cost.',
    '',
    'Read before you cite: every file you name must be one you actually opened.',
    (DRY ? '' : `\nHEARTBEAT (drive liveness): post a beat immediately after your first tool call, then every 5-8 tool calls: \`curl -s -m5 -X POST '${API_BASE}/api/drive-heartbeat/beat?project=prism' -H 'Content-Type: application/json' -d '{"task_id":"${locate.task_id}","step":"graph","elapsed_s":<seconds>,"last_tool":"<tool>","work_units":<increasing counter>}'\` - best-effort, never let a failed beat block the step.`),
  ].filter(Boolean).join('\n'),
  { label: 'graph-blast-radius', phase: 'Graph', schema: GRAPH_SCHEMA, model: TIER_MODEL.balanced })

GRAPH_BRIEF = [
  '',
  'GRAPH BRIEF (call-graph derived - this is the slice\'s real blast radius, not a guess):',
  `- entities: ${(graph.entities || []).join(', ') || '(none resolved)'}`,
  `- blast-radius files (candidate allowed_files): ${(graph.blast_radius_files || []).join(', ') || '(none)'}`,
  `- NEIGHBOURING SUITES that must stay green (run these at verify_green, not just task.verify): ${(graph.neighbouring_suites || []).join(', ') || '(none found)'}`,
  `- user-reachable surface the oracle must name: ${graph.user_reachable_surface || '(none found)'}`,
  ((graph.contradicted_contracts || []).length
    ? `- CONTRACTS THIS SLICE CONTRADICTS (retire them here, with a comment naming what superseded them): ${(graph.contradicted_contracts || []).join(' | ')}`
    : ''),
].filter(Boolean).join('\n')
log(`Graph: ${(graph.entities || []).length} entities -> ${(graph.blast_radius_files || []).length} blast-radius files, ${(graph.neighbouring_suites || []).length} neighbouring suite(s). Surface: ${graph.user_reachable_surface || 'none found'}. slice_count=${graph.slice_count}.`)

// -- Phase: Decompose (plan-time only, never retrofitted) -----------------
// Splitting happens ONLY after the conductor has accepted a plan, because a
// split is a plan decision. Children are CHILDREN (parent_id set), never
// board siblings. The hard lessons this encodes: the first child must be a
// walking skeleton that goes end-to-end through the REAL wiring (storage-
// first epics ship five green slices and a dead feature); an oracle that can
// be satisfied with every collaborator mocked is not an oracle; and each
// child must declare what PRODUCTION CODE CONSTRUCTS IT ("nothing yet" means
// the wiring belongs in that child).
const DECOMPOSE_SCHEMA = {
  type: 'object',
  required: ['children', 'rationale'],
  properties: {
    children: {
      type: 'array',
      description: 'created child tasks, in the order they must be driven (skeleton first)',
      items: {
        type: 'object',
        required: ['task_id', 'title', 'oracle', 'allowed_files'],
        properties: {
          task_id: { type: 'string' },
          title: { type: 'string' },
          oracle: { type: 'string' },
          proof_type: { type: 'string' },
          allowed_files: { type: 'array', items: { type: 'string' } },
          verify: { type: 'array', items: { type: 'string' } },
          constructed_by: { type: 'string', description: 'the production code that constructs/reaches this slice; "nothing yet" means the wiring is in scope for this child' },
          is_skeleton: { type: 'boolean' },
        },
      },
    },
    rationale: { type: 'string' },
    // ONE CONTRACT, FIXED HERE (epic 2d480b08, 2026-08-02). Two children of
    // that epic each invented their own name for the SAME event - one shipped
    // `task.changed` with a dict of scalar values on a dedicated /sse/tasks
    // route, the other `task_updated` with a list of field names over the
    // shared /sse/sessions stream. Both went green, both edited the same two
    // files, and merging both would have produced a red main because each
    // pinned its own name in its own test. Disjoint ALLOWLISTS do not make
    // slices independent when they meet at a seam; the seam has to be named
    // once, up here, before any child is created.
    shared_contract: {
      type: 'string',
      description: 'The exact names crossing the seam between children - event/type names, route paths, payload shape, function signatures - written once and quoted verbatim into EVERY child description. Empty only if the children touch no shared seam at all.',
    },
  },
}

function decomposePrompt() {
  const tid = locate.task_id
  return [
    preamble('sm'),
    '',
    `PLAN-TIME DECOMPOSITION for task ${tid}. The conductor has ACCEPTED a plan whose blast radius spans disjoint surfaces (slice_count=${graph.slice_count}). Split it into at most ${MAX_CHILDREN} CHILD tasks.`,
    '',
    ctx,
    GRAPH_BRIEF,
    '',
    'RULES (each one is a scar - do not relax them):',
    `- Every child MUST pass parent_id="${tid}". A child is never a root/board task. Titles are human-friendly WHAT, roughly 4-9 words; mechanics go in the description.`,
    '- ORDER MATTERS: the FIRST child is a WALKING SKELETON that goes end-to-end through the REAL wiring (connect -> use -> one visible result), never the storage layer first. An epic that shipped store, adapters, webhooks and UI as five green slices could not run at all: no adapter was registered in production because every test injected one.',
    '- Each child declares `constructed_by`: the production code that actually constructs or reaches that slice. If the honest answer is "nothing yet", the wiring belongs IN that child - say so and scope it there.',
    '- Each child gets an ORACLE that names a USER-REACHABLE surface. An oracle satisfiable with every collaborator mocked is not an oracle. Set proof_type to match it.',
    '- allowed_files MUST be DISJOINT across children (parallel workers are only safe with disjoint allowlists), and must include the child\'s own test file. Never put a control_plane.POLICY_FILES entry in a child\'s allowlist - that child would fail its own gates on the candidate-controls-judge tooth; put policy consumers in a separate non-policy file.',
    '- `verify` entries are WORKSPACE-ROOT-RELATIVE paths (e.g. `services/prism-service/tests/unit/test_x.py`), never `cd ... && pytest ...`. A non-relative verify makes the red check exit 4 "no tests ran".',
    '- Set stop_if: needs a file outside allowed_files; behavior ambiguous; verification fails twice.',
    '- NAME THE SEAM ONCE. If two children meet at a seam (an event type, a route path, a payload shape, a function signature), decide those exact names HERE, return them in `shared_contract`, and quote that text verbatim into EVERY child\'s description as a contract they must consume and must NOT redesign. Epic 2d480b08 skipped this: one child shipped `task.changed` (dict of scalar values, dedicated /sse/tasks route), its sibling shipped `task_updated` (list of field names, shared /sse/sessions stream), both went green on their own test, and merging both would have made main red. Disjoint allowlists did not save it, because the collision was in the NAMES, not the files.',
    '',
    `CREATE them with task_create(parent_id="${tid}", title=..., description=..., oracle=..., proof_type=..., allowed_files=[...], verify=[...], stop_if=[...], likely_misfire=<how this slice could pass but be WRONG>). Return the created ids in drive order, skeleton first.`,
    'If, having thought it through, ONE slice is genuinely the right shape after all, return children:[] and say why. Splitting has real cost and a wrong split is worse than none.',
  ].join('\n')
}

function childDriverPrompt(child, idx) {
  return [
    preamble('dev'),
    '',
    `DRIVE CHILD SLICE ${idx + 1}: "${child.title}" (id ${child.task_id})${child.is_skeleton ? ' - THIS IS THE WALKING SKELETON: it must go end-to-end through the REAL production wiring, not an injected collaborator.' : ''}`,
    `ORACLE: ${child.oracle}`,
    `ALLOWED FILES (hard scope, enforced server-side): ${(child.allowed_files || []).join(', ')}`,
    `CONSTRUCTED BY: ${child.constructed_by || '(unstated - if nothing constructs it yet, the wiring is in THIS slice)'}`,
    '',
    `HEARTBEAT (drive liveness - REQUIRED, not optional; this slice runs its own multi-step conductor_work loop and is the longest-running agent in the whole flow, so without beats the board reads it dead for its entire drive). Post your FIRST beat immediately after your first tool call, then re-beat every 5-8 tool calls (at least every ~2 minutes): \`curl -s -m5 -X POST '${API_BASE}/api/drive-heartbeat/beat?project=prism' -H 'Content-Type: application/json' -d '{"task_id":"${child.task_id}","step":"<your own current job.step from the inner loop below, not a value fixed here>","elapsed_s":<seconds since you started>,"last_tool":"<the tool you just ran>","work_units":<counter that strictly increases>}'\` - key it to child.task_id (this slice's OWN id), never the parent/epic's id, and always report the LIVE current step since this prompt is generated once before your inner loop even starts. Best-effort; a failed beat is non-fatal but must not be skipped.`,
    '',
    'Drive this child to a genuinely-evidenced green_gate with the SAME loop the parent uses:',
    `1. \`job = conductor_work(id="${child.task_id}")\` (no outcome) - this enters the flow AND creates the CHILD'S OWN git worktree. Use workspace.path for every edit/test/commit; the gate verifier reads that exact path.`,
    '2. While not done: do EXACTLY job["instructions"], produce job["expected_proof"], then `conductor_work(id=..., outcome="pass", proof=<the artifact>, model=<your model>)`. Never name a step yourself; never call conductor_advance/conductor_gate.',
    '3. GATES ARE NOT YOURS TO CLEAR. Do not report an outcome on a gate job and never pass override. Make the evidence real, then poll `/api/conductor/gate/readiness?task_id=<id>&project=prism` and re-peek with conductor_work until a distinct seat decides. If it will not clear, report exactly which tooth is unsatisfied.',
    '4. Commit the failing tests ALONE first (tests-only commit, `[task:<id8>]` trailer) - the red seat anchors there. Then implement. Then run the FULL relevant suite including the neighbouring suites named below.',
    GRAPH_BRIEF,
    '',
    'Report ok:false with the blocking tooth in halt_reason rather than forcing anything. A red test is yours, never "pre-existing".',
    telemetryInstr('child_drive', 'dev'),
  ].join('\n')
}

// -- Phase: Drive (the pull loop) ----------------------------------------
// The whole state machine, on the client side, is now this: ask the server
// what to do, do it, report the artifact, repeat until the server says done.
phase('Drive')
const trace = []
let halted = null
let decomposition = null
// Set the moment real children exist. After that the PARENT STOPS BUILDING:
// its remaining steps assemble and verify what the children shipped. Epic
// 2d480b08 had no such flag, so it drove its own implement_tasks to a
// complete implementation AND spawned children that rebuilt the same seam a
// different way - 1.85M tokens, 105 minutes, and two incompatible versions of
// one feature that could not both be merged.
let childrenOwnTheBuild = false
let done = false
let job = {
  step: locate.current_step || '',
  kind: GATE_STEPS.includes(locate.current_step) ? 'gate' : 'agent',
  role: '',
  gate_state: locate.gate_state || 'none',
  instructions: '',
  expected_proof: '',
  contract: {},
}

// STALL GUARD: a step that hands back ITSELF is not progress. Three
// consecutive turns on the same step means the drive is looping (a rubric it
// cannot satisfy, a gate no seat will decide), and looping quietly is how a
// tile ends up claiming "working" while nothing moves. Halt with the reason
// instead - "we can't see it" and "it's broken and needs you" must read
// differently, and only the second one is worth a human's attention.
const seen = {}
for (let i = 0; i < MAX_JOBS; i++) {
  const isGate = job.kind === 'gate' || GATE_STEPS.includes(job.step)
  const phaseName = isGate ? 'Gate' : 'Drive'
  seen[job.step] = (seen[job.step] || 0) + 1
  if (seen[job.step] > 3) {
    halted = {
      at: job.step,
      kind: isGate ? 'gate' : 'agent',
      reason: `stalled: "${job.step}" was handed back 3 times without advancing. `
        + (isGate
          ? 'The gate is not being decided - read the live readiness and say which tooth is unsatisfied and who must act.'
          : 'The step keeps failing its validation - the last evidence is in the trace.'),
      gate_state: job.gate_state,
    }
    break
  }
  phase(phaseName)
  const res = await agent(
    isGate ? gatePrompt(job) : workerPrompt(job),
    { label: job.step || 'peek', phase: phaseName, schema: JOB_RESULT_SCHEMA,
      model: modelFor(job.role, job.step, isGate) })
  trace.push(res)
  await postAgentRun(res, { role: job.role || '', step: job.step })

  // BUDGET HALT (task dcbd284f) - checked BEFORE the generic ok:false branch
  // so a step that ran out of time is reported as a bounded stop with its
  // duration, not as an anonymous failure over work that may be committed.
  if (res && res.budget_exceeded === true) {
    halted = budgetHalt(
      job.step, stepBudgetFor(job.step),
      Number(res.elapsed_s) || 0, res.step_retries,
    )
    if (res.halt_reason) halted.reason += ` The step was waiting on: ${res.halt_reason}`
    halted.result = res
    break
  }

  if (!res || res.ok !== true) {
    halted = {
      at: job.step,
      kind: isGate ? 'gate' : 'agent',
      reason: (res && res.halt_reason) || 'step reported ok:false',
      gate_state: (res && res.gate_state) || job.gate_state,
      result: res,
    }
    break
  }
  if (res.done === true) {
    done = true
    log(`Server reports DONE - the terminal green_gate passed for "${locate.title}".`)
    break
  }

  // DECOMPOSITION HOOK - a split is a PLAN decision, so it happens exactly
  // once, right after the conductor accepted the plan. Never retrofitted.
  if (!decomposition && MAX_CHILDREN > 0 && !DRY
      && job.step === 'verify_plan' && Number(graph.slice_count) > 1) {
    phase('Decompose')
    decomposition = await agent(decomposePrompt(),
      { label: 'decompose', phase: 'Decompose', schema: DECOMPOSE_SCHEMA, model: TIER_MODEL.frontier })
    const kids = (decomposition.children || []).slice(0, MAX_CHILDREN)
    if (kids.length === 0) {
      log(`Decomposition declined: ${decomposition.rationale}. Driving as one slice.`)
    } else {
      log(`Decomposed into ${kids.length} child slice(s), skeleton first: ${kids.map((k) => k.title).join(' | ')}`)
      childrenOwnTheBuild = true
      phase('Children')
      // SKELETON FIRST, THEN FAN OUT. The skeleton (kids[0]) must prove the
      // REAL wiring end-to-end before anything builds on it - that is the
      // 0784729f lesson, where 5 slices went green with every collaborator
      // mocked and assembled into a feature that could not run at all. But
      // that rationale only justifies the skeleton being FIRST. Slices 2..N
      // are disjoint by construction (the decomposer's contract) and each
      // drives its OWN worktree, so serializing them bought nothing and cost
      // the wall clock of the whole tail.
      const driveChild = async (kid, idx) => {
        const cres = await agent(childDriverPrompt(kid, idx),
          { label: `child:${kid.task_id.slice(0, 8)}`, phase: 'Children',
            schema: JOB_RESULT_SCHEMA, model: TIER_MODEL.fast })
        await postAgentRun(cres, { role: 'dev', step: `child:${kid.task_id.slice(0, 8)}` })
        if (!cres || cres.ok !== true) {
          log(`Child "${kid.title}" did not reach green: ${(cres && cres.halt_reason) || 'no reason given'}. The parent's own oracle still needs its own receipt - continuing the parent drive so the epic is not reported delivered on unexercised children.`)
        }
        return { child: kid, result: cres }
      }
      const skeleton = await driveChild(kids[0], 0)
      const rest = kids.length > 1
        ? await parallel(kids.slice(1).map((k, i) => () => driveChild(k, i + 1)))
        : []
      if (kids.length > 1) log(`Skeleton settled; drove the remaining ${kids.length - 1} disjoint slice(s) CONCURRENTLY.`)
      decomposition.results = [skeleton, ...rest.filter(Boolean)]
    }
  }

  if (STOP_AFTER && job.step === STOP_AFTER) {
    log(`Reached stop_after="${STOP_AFTER}" - halting the drive as requested.`)
    break
  }
  if (!res.next_step) {
    halted = { at: job.step, kind: isGate ? 'gate' : 'agent', reason: 'the server returned no next step and did not report done', result: res }
    break
  }
  job = {
    step: res.next_step,
    kind: res.next_kind || (GATE_STEPS.includes(res.next_step) ? 'gate' : 'agent'),
    role: res.next_role || '',
    gate_state: res.gate_state || 'none',
    instructions: '',
    expected_proof: '',
    contract: {},
  }
}

// -- Phase: Settle (read the terminal state back FROM THE SERVER) ---------
// The drive does not get to narrate its own outcome. This reads the live
// receipt and reports what the server actually holds - a pass is a claim
// about evidence, so the claim has to come from where the evidence lives.
const SETTLE_SCHEMA = {
  type: 'object',
  required: ['workflow_step', 'gate_state', 'status', 'receipt_ok', 'receipt_summary', 'owner_action'],
  properties: {
    workflow_step: { type: 'string' },
    gate_state: { type: 'string' },
    status: { type: 'string', description: 'task.status as stored' },
    receipt_ok: { type: 'boolean', description: 'from live readiness - whether a plain Approve would pass the evidence tooth' },
    receipt_summary: { type: 'string', description: 'the receipt as it actually reads: adapter, tree sha, and whether that tree is THIS task\'s. Quote it; never paraphrase a pass.' },
    commits: { type: 'string', description: 'the [task:<id8>] commits in the task worktree, oneline - or "none"' },
    owner_action: { type: 'string', description: 'exactly what the human must do now, with the task URL - or "none, the server decided it" ' },
    // DONE MEANS SHIPPED. Epic 2d480b08 passed green_gate and closed with
    // status=done, full_outcome_complete=true, while its PR sat open and main
    // never contained a line of it. The epic-rollup adapter counts CHILD
    // STATUS and has no shipping tooth, so nothing in the gate could catch it.
    // The drive reports it instead of inheriting the lie.
    shipped: { type: 'boolean', description: 'TRUE only if this task\'s commits are ancestors of origin/main. A passed gate is "verified", never "shipped".' },
    shipping_state: { type: 'string', description: 'where the work actually is: branch, PR number and state, whether origin/main contains it, and what still has to happen to release it' },
    evidence_cited: { type: 'string', description: 'every file from /api/tasks/<id>/evidence with its cited flag. An UNCITED file does not reach the gate card - the owner signs without seeing it.' },
  },
}
phase('Settle')
const settle = DRY ? null : await agent(
  [
    preamble('analyst'),
    '',
    `SETTLE task ${locate.task_id}. Report the SERVER'S state, not the drive's opinion of it. Read, do not mutate: no conductor_work report, no gate call, no task_update.`,
    '',
    `1. task_list(id="${locate.task_id}") -> workflow_step, gate_state, status, proof_type.`,
    `2. curl -s -m10 "${API_BASE}/api/conductor/gate/readiness?task_id=${locate.task_id}&project=prism" -> receipt_ok and the live reason. Better than task.gate_reason (a stale snapshot that has misled a driver for three turns straight) but READINESS IS NOT THE DECIDER: it is a SEPARATE implementation from gate_decide and the two DISAGREE. On 7feed0c8 readiness said receipt_ok:true "Approve to release" while gate_decide refused the same gate on a stale receipt; the owner clicked five times and the refusal drove the gate to 'failed'. NEVER report "the Approve is clean" on readiness alone - prove it with step 3.`,
    `3. OPEN THE RECEIPT before calling anything green: quote the adapter and the tree sha, and confirm that tree is THIS task's worktree${WS ? ` (\`git -C "${WS}" rev-parse HEAD\`)` : ''}. A green receipt naming another task's tree is not a pass - say so plainly if you find one. FRESHNESS IS THE SAME CHECK: a receipt is pinned to the tree it was minted at, so ANY commit after the mint (even a correct fix) invalidates it. If the newest receipt's tree_sha != worktree HEAD, say STALE and give the one-line repair: \`curl -s -m300 -X POST "${API_BASE}/api/conductor/gate/mint" -H 'Content-Type: application/json' --data-binary '{"task_id":"${locate.task_id}"}'\` (seconds, not minutes). Re-mint IN THE SAME BREATH as any commit you add, never leave it for the owner's click to discover.`,
    WS ? `4. \`git -C "${WS}" log --oneline -8\` -> the commits this drive actually produced. A drive that reports done with no [task:<id8>] commit produced nothing.` : '',
    `5. owner_action: if the gate is decided, "none, the server decided it". If a human must click, give the one sentence they need and the URL ${API_BASE}/tasks/${locate.task_id}?project=prism. If readiness says the review IS the sign-off AND step 3 confirmed the receipt is fresh at this tree, say THAT - do not tell a human they are blocked when they are not. NEVER tell them to tick Override: gate_decide re-runs the oracle even with override=True (it forgives only a literal manual_evidence_required reason backed by a matching manual receipt), so against a stale receipt Override can NEVER succeed and the refused attempt can drive the gate to 'failed'. The correct sentence is always "re-run the oracle, then Approve with Override UNTICKED".`,
    `6. EVIDENCE MUST BE CITED, not merely written. curl -s -m10 "${API_BASE}/api/tasks/${locate.task_id}/evidence?project=prism" and report every file WITH its \`cited\` flag. A file sitting there with cited=false never reaches the gate card, so the owner signs off without ever seeing it - that happened on epic 2d480b08 and was caught only because the owner pushed back. If anything is uncited, that is an owner_action, not a footnote: cite it from completion_proof.`,
    `7. SHIPPING, decided by git and not by the gate: \`git -C "${WS || '.'}" log --oneline -1\`, then check whether those commits are ancestors of origin/main (\`git merge-base --is-ancestor <sha> origin/main\`), and look for an open PR (\`gh pr list --head <branch>\`). Set shipped=true ONLY if origin/main actually contains the work.`,
    '',
    'DONE MEANS SHIPPED, AND THIS IS THE DONE GATE - WAIT HERE. A passed green_gate is "verified", not released.',
    'Epic 2d480b08 went status=done with full_outcome_complete=true while its PR sat open and main had none of it: the epic-rollup adapter counts child status and has no shipping tooth, so the gate could not catch it and the board showed a delivered feature that did not exist anywhere a user could reach.',
    'So: never report a task as done or shipped while its commits sit on an unmerged branch. Say plainly that it is VERIFIED BUT NOT RELEASED, give the exact remaining action, and let the human decide. Reporting "done" on unmerged work is the single failure this step exists to prevent.',
  ].filter(Boolean).join('\n'),
  { label: 'settle', phase: 'Settle', schema: SETTLE_SCHEMA, model: modelFor('', 'settle', false) })

// -- Report ---------------------------------------------------------------
return {
  task_id: locate.task_id,
  title: locate.title,
  workspace: WS,
  branch: locate.branch,
  dry_run: DRY,
  // `done` is the SERVER's terminal-gate answer. It is NOT "shipped" - keep
  // the two separate at the top level so a caller cannot read one as the
  // other, which is exactly how epic 2d480b08 got reported as delivered while
  // origin/main contained none of it.
  done,
  shipped: !!(settle && settle.shipped === true),
  verified_not_released: !!(done && settle && settle.shipped !== true),
  shipping_state: (settle && settle.shipping_state) || '(not checked)',
  started_at_step: locate.current_step || '(not entered)',
  ended_at_step: (settle && settle.workflow_step) || (trace.length ? trace[trace.length - 1].next_step : locate.current_step),
  graph: {
    entities: graph.entities,
    blast_radius_files: graph.blast_radius_files,
    neighbouring_suites: graph.neighbouring_suites,
    user_reachable_surface: graph.user_reachable_surface,
    contradicted_contracts: graph.contradicted_contracts || [],
    slice_count: graph.slice_count,
  },
  decomposition: decomposition
    ? {
      rationale: decomposition.rationale,
      children: (decomposition.children || []).map((c) => ({ task_id: c.task_id, title: c.title, oracle: c.oracle, is_skeleton: !!c.is_skeleton })),
      outcomes: (decomposition.results || []).map((r) => ({ task_id: r.child.task_id, title: r.child.title, ok: !!(r.result && r.result.ok), halt_reason: (r.result && r.result.halt_reason) || '' })),
    }
    : null,
  steps_driven: trace.map((t) => ({ step: t.step, ok: t.ok, next_step: t.next_step, gate_state: t.gate_state, source_tier: t.source_tier })),
  halted,
  // THE RESUME CONTRACT (owner 2026-08-13: a drive halted at plan_gate sat
  // driverless for an hour after the owner approved - "you did it again").
  // A halt at one of the owner's two gates is NOT the end of the work, only
  // of this run. The INVOKER (the main thread that called Workflow) must not
  // walk away: watch the gate (poll the readiness/gate_state below, or set a
  // Monitor on it) and the moment it clears, relaunch this workflow FRESH
  // (never resumeFromRunId - cached pre-flight verdicts replay stale) with
  // the same task_id/api_base so implementation continues without a human
  // having to nudge it.
  resume: (halted && halted.kind === 'gate' && HUMAN_GATES.includes(halted.at))
    ? {
      must_resume: true,
      watch: `${API_BASE}/api/conductor/gate/readiness?task_id=${(locate && locate.task_id) || TASK_ID}&project=prism`,
      when: `task gate_state for ${halted.at} reads 'passed' (owner approved)`,
      relaunch: { name: 'implement', args: { task_id: (locate && locate.task_id) || TASK_ID, api_base: API_BASE } },
    }
    : null,
  settle,
  trace,
}
