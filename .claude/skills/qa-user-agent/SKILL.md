---
name: qa-user-agent
description: Validate PRISM's own web UI like a real user -- walk stated acceptance criteria one at a time with fresh screenshots/console/network checks, never claim something works from source-reading or a passing test suite alone. Drives via `agent-bridge-drive` (remote assist, the owner's real live tab) by DEFAULT; falls back to this skill's own Playwright MCP browser only when unattended or no live session exists. Use when asked to "QA this", "validate as a user", "does this actually work", "test the app like a user would", or before attaching evidence to a green_gate/demo proof. For a fast single-change confirmation with no gate/evidence rigor, use the `run` skill instead.
version: 1.0.0
---

# QA user agent -- validate PRISM's web UI like a real user

## Why this exists

Reading the TSX source, or a green pytest run, tells you the code exists --
neither tells you a person can reach it, that it renders, or that clicking it
doesn't throw. This skill is the missing step: actually drive the running
app in a real browser and record what you observed, not what you expect.

## Before you touch the browser: confirm you're testing the REAL thing

- Identify which live instance you're pointed at (see this project's
  CLAUDE.md "Service ports" table -- AOS-hosted dev, Windows dev, release)
  and confirm with a live `curl .../api/version`, never assumed from docs.
- `GET /api/version` returns `web_build` -- a hash of `index.html`'s
  mtime+size. If you just landed a TSX change, confirm `web_build` reflects
  a build made AFTER that change's timestamp -- if stale, rebuild
  (`npm run build` inside `prism_service/web`) and re-check before testing.
  A bumped `PRISM_VERSION` string alone proves nothing about the browser
  bundle; `web_build` is the one honest signal of what's actually served.

## Get what "correct" means before you touch the browser

- If there's a task_id: pull its ACs from the live plan (`task_list` with
  `fields=["plan_doc"]`, or the task envelope's `task.plan_doc` -- never the
  `gate_reason` string, which is a stale snapshot). If `has_prototype` is
  true, fetch `GET /api/tasks/<id>/prototype?project=<proj>` -- that is the
  approved design; plan_doc prose can and does drift from it.
- No ticket at all? State the golden-path user story explicitly before
  driving ("a user wants to X, starting from Y") so "done" has a definition
  before you start clicking around.

## Which mechanism drives the browser

**Default to `agent-bridge-drive`** -- it drives the owner's own real,
already-logged-in tab; that is the whole reason it exists, and it means
you're testing the actual authenticated session, not a synthetic one.
Fall back to this skill's own Playwright MCP control only when:

- there's no live bridge session (unattended/background work, nobody has
  a tab open), or
- you need console/network error capture -- agent-bridge cannot do this
  YET (`navigate`/`click`/`fill`/`read`/`screenshot` only; console/network
  actions are tracked as epic 1d252db6's first child, task 4f5cc773).
  Until that ships, drive the interaction via agent-bridge as normal but
  do the console/network check for that same interaction in a Playwright
  tab pointed at the same URL, and say plainly that's what you did.

## Drive it like a user, not an API client

(Read these with either mechanism -- swap `mcp__playwright__browser_*` for
the matching `agent_bridge_command` action when driving via the bridge.)

- Navigate to the app root and reach the feature via the REAL entry point
  -- the nav link, button, or route a person actually clicks. Never
  deep-link straight to a route just to prove a component exists;
  unreachable-but-present is not done (a feature with no nav entry is
  invisible to every real user, regardless of what the code does).
- Read structured state after every action (`browser_snapshot`, or
  agent-bridge's `read`) rather than reasoning from what you expect to
  have happened -- re-check AFTER, don't assume before.
- If a click looks like a no-op, look for a covering/blocking element
  before retrying. Silently retrying until something "works" hides a real
  bug -- report the blocking element by name instead.
- Only act on an element from your MOST RECENT read/snapshot, never one
  from several steps back -- the DOM can change under you (a reload, a
  re-render, a route change), and acting on a stale reference can hit a
  different element than the one you actually observed. Re-read before
  every act, not just after.

## Walk acceptance criteria ONE AT A TIME

For each AC, in order:

1. Perform the exact user action the AC describes.
2. Take a fresh structured read + screenshot (`browser_snapshot` +
   `browser_take_screenshot`, or agent-bridge's `read` + `screenshot`).
3. Check for console errors and any 4xx/5xx network responses -- after
   THIS interaction, not just once at the end. Via Playwright:
   `browser_console_messages` + `browser_network_requests`. Via
   agent-bridge: not yet possible (see "Which mechanism" above) -- fall
   back to a Playwright tab on the same URL for this one check.
4. Record pass/fail with the concrete thing you observed, not a blanket
   "AC met."

Never summarize "all ACs pass" without having done steps 1-4 for every
single one -- a rubric-shaped claim needs a rubric-shaped receipt.

## Golden path AND edge cases

Walk the happy path fully, then at least one edge/invalid case per flow:
empty input, an oversized input, a double-click, the back button, a reload
mid-flow, a second tab. A feature that only survives the happy path has
not been validated -- it's been demoed.

## Visual/design comparison

When an approved prototype/mock exists, hand the comparison to
`qa-visual-diff` rather than eyeballing it yourself -- it forces a
surface-by-surface checklist instead of a "looks close" verdict.

## Evidence discipline

- Evidence lives IN PRISM's task evidence store, never a claude.ai artifact
  or any external host -- the owner reviews evidence where the gate is.
  There's no upload endpoint: write screenshots straight to
  `data_dir.evidence_dir(task_id)` on disk (the same directory the
  browser-oracle runner writes into) -- `GET /api/tasks/<id>/evidence`
  lists whatever's in that directory automatically.
- Save every screenshot as you go; cite the actual saved filenames in your
  completion_proof / gate proof text so the SPA's evidence list links back
  to what you did.
- Never write "verified" or "done" in a proof without the evidence path you
  produced THIS run attached. A description of what should be true is not
  evidence of what is true.

## Known failure modes to actively guard against

These have each shipped a false "done" on this exact app before -- treat
them as a checklist of ways you personally could be wrong right now:

- Testing against a stale bundle (`web_build` didn't move).
- A "browser oracle" that only loads a URL headlessly and greps for a
  literal string -- that proves a page renders, not that a user's flow
  works. Use it as a floor, never as your ceiling.
- Matching a source comment or an unrelated branch instead of the actually
  rendered tag/element.
- Proving a component exists in isolation while its real nav entry lives
  outside what you touched, so nobody can reach it.
- Treating a green unit/integration test suite as proof a user can do the
  thing -- a suite that injects every collaborator can pass while the
  assembled product is unreachable end-to-end.
- Reporting "all good" with no per-AC observations to back it.

## When NOT to use this

- A fast single-change sanity check with no gate/evidence rigor needed:
  use the `run` skill instead.
- No specific ticket, just "make sure nothing's broken" after a big
  merge/release: use `qa-regression-sweep`.

Note this skill does not compete with `agent-bridge-drive` -- it's the
checklist this skill tells you to run first. Reach past it to Playwright
only per "Which mechanism drives the browser" above.

