export const meta = {
  name: 'prs',
  description: 'Operate every open PR to conclusion through PRISM: join each PR to its [task:<id>] owner, let the TASK GATE (not CI) authorize the merge, land it, then write the merge receipt back so "done" and "merged" stop drifting apart. Reports the reverse drift too - tasks PRISM calls done whose commits never reached main. Idempotent per tick, so it is safe to /loop.',
  whenToUse: 'Run on a loop to keep the PR queue converging: `/loop 20m /prs`, or one-shot as Workflow({name:"prs", args:{api_base:"http://127.0.0.1:8888"}}). Pass {dry_run:true} to survey and classify without touching anything, {merge:false} to stop short of merging, {close_stale_drafts:true} to also close abandoned drafts, {stale_days:N} to change what counts as abandoned (default 21), {max_prs:N} to cap the per-tick fan-out (default 6), {merge_method:"merge"|"squash"|"rebase"} (default squash).',
  phases: [
    { title: 'Survey', detail: 'Join every open PR to its PRISM task; classify the drift', model: 'sonnet' },
    { title: 'Operate', detail: 'One agent per actionable PR, carries it to conclusion', model: 'sonnet' },
    { title: 'Reconcile', detail: 'Write each merge receipt back into PRISM', model: 'haiku' },
  ],
}

// -- Input normalization ------------------------------------------------
// args may arrive as a JSON string, a plain object, or nothing at all.
let _in = args
if (typeof _in === 'string') {
  try { _in = JSON.parse(_in) } catch { _in = {} }
}
_in = _in && typeof _in === 'object' ? _in : {}

// The PRISM web port this tick reads task state from. Defaults to the
// CANONICAL release port every customer runs; a dev instance passes
// api_base (http://127.0.0.1:8888 here). Never hardcode a dev port.
const API_BASE = (_in.api_base || 'http://127.0.0.1:7778').replace(/\/$/, '')
const DRY = _in.dry_run === true || _in.dry_run === 'true'
// Merging is the POINT of this workflow (owner: "operate the pr to
// conclusion ... prism should have merged these prs when the done button was
// clicked"), so it is on by default - but gated on the PRISM task, never on
// CI alone. {merge:false} turns the tick into a reporting sweep.
const MERGE = !(_in.merge === false || _in.merge === 'false')
const MERGE_METHOD = ['merge', 'squash', 'rebase'].includes(_in.merge_method)
  ? _in.merge_method : 'squash'
// Closing someone's draft is not reversible from here, so it is OPT-IN per
// tick even though a stale draft is always REPORTED.
const CLOSE_STALE = _in.close_stale_drafts === true || _in.close_stale_drafts === 'true'
const STALE_DAYS = Number.isFinite(Number(_in.stale_days))
  ? Math.max(3, Math.min(365, Number(_in.stale_days))) : 21
// Per-tick fan-out cap. A loop tick that tries to work 21 PRs at once is how
// a background loop turns into an unbounded spend; anything over the cap is
// LOGGED as deferred, never silently dropped, and the next tick picks it up.
const MAX_PRS = Number.isFinite(Number(_in.max_prs))
  ? Math.max(1, Math.min(12, Number(_in.max_prs))) : 6

// -- Verdicts -----------------------------------------------------------
// The survey assigns exactly one verdict per PR and the operate phase is
// allowed to take exactly the action that verdict names. Keeping the
// decision in the survey (and the ACTION in the operator) is what stops a
// per-PR agent from talking itself into a merge.
//
//   ship            PRISM task is signed off + CI green + mergeable -> MERGE
//   awaiting_prism  green + mergeable, but the task is NOT signed off ->
//                   report which gate is owed. NEVER merged: PRISM's gate is
//                   the merge authority, a green CI run is not.
//   conflicted      needs a rebase onto main before anything else
//   red             CI is failing -> diagnose and push a fix
//   unlinked        no [task:<id>] owner -> PRISM cannot authorize it
//   stale           untouched > stale_days and going nowhere -> propose close
const ACTIONABLE = ['ship', 'conflicted', 'red', 'stale']

// A PR whose operator keeps coming back empty - refused for want of consent
// (the #225 rebase+force-push), or dying on a terminal error - costs a full
// agent EVERY tick to fail the same way. After this many consecutive blocked
// ticks the loop stops spawning an operator for it and just keeps reporting
// it, so quiet ticks settle back to roughly one survey agent. The count lives
// in a small file under the OS temp dir (the script itself cannot do I/O);
// deleting that file, or a tick where the PR is worked successfully, resets
// it. 0 disables the skip and restores retry-every-tick.
const BLOCK_LIMIT = Number.isFinite(Number(_in.block_limit))
  ? Math.max(0, Math.min(10, Number(_in.block_limit))) : 2
const BLOCK_STATE = 'prism-prs-blocked.json'

const SURVEY_SCHEMA = {
  type: 'object',
  required: ['prs', 'orphan_tasks', 'headline'],
  properties: {
    prs: {
      type: 'array',
      items: {
        type: 'object',
        required: ['number', 'title', 'branch', 'draft', 'mergeable', 'ci',
                   'age_days', 'task_id', 'task_status', 'verdict', 'why'],
        properties: {
          number: { type: 'integer' },
          title: { type: 'string' },
          branch: { type: 'string' },
          draft: { type: 'boolean' },
          mergeable: { type: 'string', description: 'MERGEABLE | CONFLICTING | UNKNOWN' },
          ci: { type: 'string', description: 'SUCCESS | FAILURE | PENDING | NONE' },
          age_days: { type: 'integer', description: 'days since the PR was last updated' },
          task_id: { type: 'string', description: 'full PRISM uuid, or "" when the PR names no task' },
          task_title: { type: 'string' },
          task_status: { type: 'string', description: 'pending|in_progress|blocked|done|cancelled, or "" when unlinked' },
          signed_off: { type: 'boolean', description: 'true only when the PRISM task actually cleared its green_gate' },
          gate_reason: { type: 'string', description: 'the task gate receipt, verbatim, when there is one' },
          verdict: { type: 'string', enum: ['ship', 'awaiting_prism', 'conflicted', 'red', 'unlinked', 'stale'] },
          blocked_count: { type: 'integer', description: 'consecutive prior ticks whose operator for this PR came back empty; 0 when absent from the state file' },
          why: { type: 'string', description: 'one line: the evidence behind this verdict' },
        },
      },
    },
    orphan_tasks: {
      type: 'array',
      description: 'REVERSE drift: PRISM tasks marked done whose commits are not on origin/main and have no open PR',
      items: {
        type: 'object',
        required: ['task_id', 'title', 'detail'],
        properties: {
          task_id: { type: 'string' }, title: { type: 'string' }, detail: { type: 'string' },
        },
      },
    },
    headline: { type: 'string', description: 'one sentence a human can read without opening anything' },
  },
}

const OPERATE_SCHEMA = {
  type: 'object',
  required: ['number', 'verdict', 'action_taken', 'ok', 'detail'],
  properties: {
    number: { type: 'integer' },
    verdict: { type: 'string' },
    action_taken: { type: 'string', enum: ['merged', 'rebased', 'fixed', 'closed', 'nothing', 'halted'] },
    ok: { type: 'boolean' },
    detail: { type: 'string', description: 'what actually happened, with the git/gh evidence' },
    merge_sha: { type: 'string', description: 'the commit on main, when this PR was merged' },
    task_id: { type: 'string' },
    needs_human: { type: 'string', description: 'the one thing a person must decide, or "" ' },
  },
}

// -- Survey -------------------------------------------------------------
phase('Survey')

const survey = await agent(`You are surveying the OPEN PULL REQUESTS of this repo and joining each one
to the PRISM task that owns it. READ ONLY - you make no changes at all.

1. \`gh pr list --state open --limit 100 --json number,title,isDraft,mergeable,updatedAt,headRefName,statusCheckRollup\`
   Compute age_days for each PR from updatedAt against \`date -u +%Y-%m-%d\` (you have a
   real clock through bash; the calling script does not).

2. Find each PR's OWNING PRISM TASK. The id appears as a \`[task:<id>]\` trailer in the PR
   title or in its commit subjects (\`gh pr view <n> --json commits\`). The trailer usually
   carries only the FIRST 8 HEX CHARS, and the PRISM task API does NOT resolve a short id -
   you must match it as a PREFIX against the full uuids:
     curl -s "${API_BASE}/api/tasks?project=prism&status=all"
   From the matched task read: id (full uuid), title, status, gate_reason.
   If the daemon does not answer, say so in \`headline\` and set every task field to "" -
   NEVER guess a task's status, and never mark a PR \`ship\` on a guess.

3. Decide \`signed_off\`: true ONLY when the task's own gate actually cleared - status is
   "done", or gate_reason is a real approval receipt. A green CI run is NOT sign-off.
   PRISM's gate is the merge authority here; CI is only a precondition.

4. Assign exactly one verdict per PR:
     ship            = signed_off AND ci==SUCCESS AND mergeable==MERGEABLE AND not draft
     awaiting_prism  = ci==SUCCESS AND mergeable==MERGEABLE AND NOT signed_off
                       (the code is ready and the TASK still owes a gate - say which)
     conflicted      = mergeable==CONFLICTING and the task is still live (not stale)
     red             = ci==FAILURE and the task is still live
     stale           = age_days > ${STALE_DAYS} AND (draft OR conflicting) AND no live task
     unlinked        = no [task:<id>] owner could be resolved, but it is NOT stale
   PRECEDENCE when several fit: ship > awaiting_prism > stale > conflicted > red > unlinked.
   \`stale\` deliberately outranks conflicted, red AND unlinked: an abandoned branch is not
   worth a rebase, a CI fix, or a report saying only that nobody owns it. An old draft with
   no task is the ARCHETYPAL stale PR, not an interesting unlinked one - if you classify a
   60-day-old conflicting draft as \`unlinked\` you have mis-sorted it.

5. orphan_tasks - the REVERSE drift, capped at 10 and report-only: PRISM tasks with
   status "done" whose work never landed. Cheap check: for each \`prism/ws/*\` or task
   workspace branch, \`git log origin/main..<branch> --oneline\` has commits AND no open PR
   above carries that branch. Skip this step entirely if it takes more than ~60s.

6. BLOCKED-TICK COUNTS. Read \`"$(dirname "$(mktemp -u)")/${BLOCK_STATE}"\` if it exists - a
   JSON object of {"<pr number>": <consecutive blocked ticks>} written by earlier ticks.
   Set \`blocked_count\` per PR from it (0 when the file or the key is absent). This is the
   ONE file you may read outside the repo; do not write it, a later stage owns that.

SCRATCH FILES: this checkout is SHARED with other live sessions and this workflow runs on a
LOOP. Never write intermediate files into the repo (no tasks_dump.json, no id lists in the
working tree) - a tick that leaves a 2MB dump behind does it again every 20 minutes and
pollutes everyone else's \`git status\`. Spill anything you need under the OS temp dir
(\`mktemp -d\`) and leave the checkout exactly as you found it.

Be exact and terse. \`why\` is one line of evidence per PR, not a paragraph.`,
  { label: 'survey:open-prs', phase: 'Survey', schema: SURVEY_SCHEMA, model: 'sonnet' })

if (!survey || !Array.isArray(survey.prs)) {
  return { ok: false, headline: 'survey failed - no PR state could be read this tick', prs: [] }
}

const all = survey.prs
const blocked = all.filter(p => p.verdict === 'awaiting_prism')
const unlinked = all.filter(p => p.verdict === 'unlinked')
// The owner's actual worry, called out by name: PRISM says done, main does not.
const drift = all.filter(p => p.task_status === 'done' && p.verdict !== 'ship')
  .concat(blocked.filter(p => p.task_status === 'done'))

// Closing a stale PR is ONE `gh pr close` that touches no code, so the whole
// sweep is a single agent. Only work that reaches into the repo (merge,
// rebase, CI fix) earns a per-PR agent with its own worktree - otherwise a
// backlog cleanup spends 18 agents on 18 one-line commands.
const staleQueue = CLOSE_STALE ? all.filter(p => p.verdict === 'stale').slice(0, 25) : []
let queue = all.filter(p => ACTIONABLE.includes(p.verdict) && p.verdict !== 'stale')
if (!MERGE) queue = queue.filter(p => p.verdict !== 'ship')
// Park the PRs whose operator has come back empty BLOCK_LIMIT ticks running.
// Still reported every tick - parked is not forgotten - just not re-attempted.
const skippedBlocked = BLOCK_LIMIT
  ? queue.filter(p => (p.blocked_count || 0) >= BLOCK_LIMIT) : []
if (skippedBlocked.length) {
  queue = queue.filter(p => (p.blocked_count || 0) < BLOCK_LIMIT)
  log(`parking ${skippedBlocked.length} PR(s) after ${BLOCK_LIMIT} blocked tick(s): ${skippedBlocked.map(p => '#' + p.number).join(', ')} - still reported, no operator spawned. A human decision or {block_limit:0} resumes them.`)
}
// Ship first (it retires work), then conflicts, then red.
const RANK = { ship: 0, conflicted: 1, red: 2 }
queue.sort((a, b) => (RANK[a.verdict] - RANK[b.verdict]) || (a.number - b.number))

const deferred = queue.slice(MAX_PRS)
queue = queue.slice(0, MAX_PRS)
if (deferred.length) {
  log(`deferring ${deferred.length} PR(s) past the per-tick cap of ${MAX_PRS}: ${deferred.map(p => '#' + p.number).join(', ')} - the next tick takes them`)
}

log(survey.headline || `${all.length} open PRs surveyed`)
if (blocked.length) {
  log(`${blocked.length} PR(s) are code-ready but NOT merged: their PRISM task still owes a gate - ${blocked.map(p => '#' + p.number).join(', ')}`)
}

const staleCount = all.filter(p => p.verdict === 'stale').length
if (staleCount && !CLOSE_STALE) {
  log(`${staleCount} PR(s) look abandoned (untouched > ${STALE_DAYS}d). Re-run with {close_stale_drafts:true} to close them; this tick left them open.`)
}

// A quiet tick is the common case on a loop: cost it at one survey agent.
if (DRY || (!queue.length && !staleQueue.length)) {
  return {
    ok: true,
    mode: DRY ? 'dry_run' : 'quiet_tick',
    headline: survey.headline,
    counts: all.reduce((m, p) => (m[p.verdict] = (m[p.verdict] || 0) + 1, m), {}),
    awaiting_prism: blocked.map(p => ({ number: p.number, task_id: p.task_id, why: p.why })),
    unlinked: unlinked.map(p => ({ number: p.number, title: p.title })),
    done_but_unmerged: drift.map(p => ({ number: p.number, task_id: p.task_id, why: p.why })),
    orphan_tasks: survey.orphan_tasks || [],
    stale: all.filter(p => p.verdict === 'stale')
      .map(p => ({ number: p.number, title: p.title, age_days: p.age_days })),
    parked: skippedBlocked.map(p => ({ number: p.number, verdict: p.verdict, blocked_count: p.blocked_count })),
    would_work: DRY
      ? queue.map(p => ({ number: p.number, verdict: p.verdict }))
        .concat(staleQueue.map(p => ({ number: p.number, verdict: 'stale' })))
      : [],
    deferred: deferred.map(p => p.number),
  }
}

// -- Operate ------------------------------------------------------------
// Every operator runs in its OWN git worktree. This checkout is SHARED with
// other live sessions, so a per-PR agent must never rebase or check out in
// it - one operator switching HEAD would corrupt every other lane's work.
phase('Operate')

const PR_RULES = `HARD RULES, all of them absolute:
  - NEVER push to main/master, never force-push anything, never \`git push --force\`.
  - NEVER merge a PR whose verdict is not exactly "ship". If you believe the verdict is
    wrong, return action_taken "halted" and say why in needs_human. You do not get to
    re-decide the merge authority: PRISM's task gate decided it, upstream of you.
  - Work ONLY inside your own worktree, on this PR's own branch. Never touch another
    branch, never reset, never rewrite published history other than a rebase of THIS
    branch onto origin/main.
  - If anything is ambiguous or a step fails twice, STOP: action_taken "halted",
    ok false, and name the decision in needs_human. A halted PR is a fine outcome;
    a guessed one is not.
  - Report what you actually observed (command + result), never what you expected.`

const drivePRs = () => pipeline(queue,
  (pr) => agent(`Carry PR #${pr.number} to its conclusion. Its verdict this tick is "${pr.verdict}".

PR #${pr.number} "${pr.title}" - branch ${pr.branch}, mergeable=${pr.mergeable}, ci=${pr.ci},
age=${pr.age_days}d, PRISM task ${pr.task_id || '(none)'} status=${pr.task_status || '(none)'}.
Survey evidence: ${pr.why}

Do EXACTLY the action for this verdict and nothing else:

  ship       -> \`gh pr merge ${pr.number} --${MERGE_METHOD} --delete-branch\`. Re-check
                immediately before merging that CI is still SUCCESS and mergeable is still
                MERGEABLE (\`gh pr view ${pr.number} --json mergeable,statusCheckRollup\`) -
                state moves between the survey and now. Then confirm the merge from GIT
                FACTS, not from the command's exit code: \`git fetch origin\` then
                \`git log origin/main --oneline -3\` must show it. Return merge_sha.

  conflicted -> In your worktree: \`git fetch origin\`, check out this PR's branch, rebase it
                onto origin/main, resolve conflicts ONLY where the intent is unambiguous,
                run the pinned checks if the repo has them, then \`git push --force-with-lease\`
                to the PR branch (never plain --force, never to main). If a conflict needs a
                judgment call about product behaviour, halt and name it.

  red        -> Read the failing run (\`gh run view <id> --log-failed\`), find the ROOT CAUSE,
                fix it on this PR's branch, push. A failing test is never "pre-existing" or
                "unrelated" - if the cause is genuinely outside this PR, halt and say so
                with the evidence. Do not disable, skip or xfail a test to go green.

  stale      -> \`gh pr close ${pr.number} --comment "<one line: why this is abandoned>"\`.
                Do NOT delete the branch - closing is reversible, deleting is not.

${PR_RULES}`,
    { label: `pr#${pr.number}:${pr.verdict}`, phase: 'Operate', schema: OPERATE_SCHEMA,
      model: 'sonnet', isolation: 'worktree' }),

  // -- Reconcile ---------------------------------------------------------
  // The whole reason this workflow exists: a merge that PRISM never hears
  // about recreates the drift it was run to close. Only merged PRs that
  // actually name a task reach an agent here; everything else costs nothing.
  (res, pr) => {
    if (!res || res.action_taken !== 'merged' || !res.ok) return res
    if (!pr.task_id) return { ...res, reconciled: 'no task id - nothing to write back' }
    return agent(`PR #${pr.number} just merged to main as ${res.merge_sha || '(sha unknown)'}.
Write that receipt back onto PRISM task ${pr.task_id} so the board stops disagreeing with git.

Load the PRISM tools first: ToolSearch "select:mcp__prism__task_list,mcp__prism__task_update".

1. Read the task: task_list(id="${pr.task_id}", fields=["id","title","status","completion_proof"]).
2. task_update it: APPEND to completion_proof (never replace what is already there) a line
   \`Shipped: PR #${pr.number} merged to main @ ${res.merge_sha || 'see git log'}\`.
   Set status "done" ONLY if it is not already done - DONE here means SHIPPED, and the
   commits are now on main, so this is the one moment that claim is true.
3. Do NOT clear, approve or decide any gate, and do not advance the conductor. You are
   recording a git fact, not adjudicating anything.

Return what you actually wrote. If the daemon is unreachable, say so plainly - the merge
still happened and this reconcile can be retried on the next tick.`,
      { label: `reconcile:${pr.number}`, phase: 'Reconcile', model: 'haiku' })
      .then(note => ({ ...res, reconciled: note }))
      .catch(err => ({ ...res, reconciled: `FAILED: ${err && err.message ? err.message : err}` }))
  },
)

// The stale sweep: one agent, one `gh pr close` per PR, no repo access.
const sweepStale = () => agent(`Close these abandoned pull requests. They were classified stale this tick:
untouched for more than ${STALE_DAYS} days, draft or conflicting, and no live PRISM task owns them.

${staleQueue.map(p => `  #${p.number} (${p.age_days}d) ${p.title} -- ${p.why}`).join('\n')}

For EACH one, run exactly:
  gh pr close <number> --comment "<one short line saying why it is being closed>"
Write the comment per PR from its own age and state, so the record explains itself later.

HARD RULES:
  - Close ONLY the numbers listed above. If a number is already closed, record that and move on.
  - Do NOT pass --delete-branch. Closing is reversible and the branch is the only copy of
    that work; deleting it is not reversible. The branches stay.
  - Do NOT merge, rebase, comment on, or reopen anything else.
  - Verify each close with \`gh pr view <number> --json state\` and report the state you SAW.

Return a plain list: one line per PR, "#<n> closed" or "#<n> NOT closed: <reason>", then a
final count.`,
  { label: `sweep:${staleQueue.length}-stale`, phase: 'Operate', model: 'haiku' })

const [worked, sweep] = await parallel([
  () => (queue.length ? drivePRs() : Promise.resolve([])),
  () => (staleQueue.length ? sweepStale() : Promise.resolve('')),
])

const results = (worked || []).filter(Boolean)
// An operator that DIED or was REFUSED (safety classifier, terminal API error)
// resolves to null, and filter(Boolean) would drop it from the digest - so the
// tick would report halted:[] and read like a clean run. Index-align against
// the queue and surface them by name: a PR nobody could work is exactly what a
// human needs to see. Observed live on #225, where the rebase+force-push was
// refused for want of explicit consent to rewrite a remote branch.
const lost = queue.filter((p, i) => !((worked || [])[i]))
if (lost.length) {
  log(`${lost.length} operator(s) returned nothing - refused or died: ${lost.map(p => '#' + p.number).join(', ')}. NOT silently dropped; see blocked[].`)
}

// Persist the blocked counts so the NEXT tick can park a PR that keeps
// failing the same way. Worked PRs have their count cleared, so a PR that
// starts succeeding is never parked on stale history.
if (queue.length && BLOCK_LIMIT) {
  await agent(`Update this loop's blocked-tick counts. The file is \`"$(dirname "$(mktemp -u)")/${BLOCK_STATE}"\`,
a JSON object of {"<pr number>": <consecutive blocked ticks>}. Create it if absent.

  INCREMENT by 1 (or set to 1 if the key is absent): ${JSON.stringify(lost.map(p => p.number))}
  DELETE these keys entirely (they were worked this tick): ${JSON.stringify(results.map(r => r.number))}

Leave every other key untouched. Write valid JSON and read it back to confirm it parses.
Touch nothing inside the git repo. Return the resulting file contents on one line.`,
    { label: 'state:blocked-counts', phase: 'Reconcile', model: 'haiku' })
}
const merged = results.filter(r => r.action_taken === 'merged' && r.ok)
const halted = results.filter(r => r.action_taken === 'halted' || !r.ok)

log(`tick complete: ${merged.length} merged, ${results.length - merged.length - halted.length} advanced, ${halted.length} halted, ${staleQueue.length} stale swept`)

return {
  ok: true,
  mode: 'operated',
  headline: survey.headline,
  merged: merged.map(r => ({ number: r.number, sha: r.merge_sha, task_id: r.task_id, reconciled: r.reconciled })),
  advanced: results.filter(r => r.ok && r.action_taken !== 'merged')
    .map(r => ({ number: r.number, action: r.action_taken, detail: r.detail })),
  halted: halted.map(r => ({ number: r.number, detail: r.detail, needs_human: r.needs_human })),
  parked: skippedBlocked.map(p => ({ number: p.number, verdict: p.verdict, blocked_count: p.blocked_count,
    note: `no operator spawned - came back empty ${p.blocked_count} tick(s) running. Needs a human decision; {block_limit:0} forces a retry.` })),
  blocked: lost.map(p => ({ number: p.number, verdict: p.verdict,
    note: 'the operator returned nothing - refused (e.g. consent needed to rewrite remote history) or died. Re-runs next tick and will keep costing a tick until a human decides.' })),
  closed_stale: staleQueue.length ? { count: staleQueue.length, numbers: staleQueue.map(p => p.number), report: sweep } : null,
  // Carried every tick so the loop keeps reporting the gap even when it can
  // do nothing about it - these are the PRs only a PRISM gate can release.
  awaiting_prism: blocked.map(p => ({ number: p.number, task_id: p.task_id, why: p.why })),
  unlinked: unlinked.map(p => ({ number: p.number, title: p.title })),
  done_but_unmerged: drift.map(p => ({ number: p.number, task_id: p.task_id, why: p.why })),
  orphan_tasks: survey.orphan_tasks || [],
  deferred: deferred.map(p => p.number),
}
