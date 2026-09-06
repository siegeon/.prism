---
name: qa-regression-sweep
description: Broad no-ticket health sweep across PRISM's whole web UI -- crawl the main surfaces, check console/network on each, diff against the previous sweep's screenshots. Use for "make sure nothing's broken", "smoke test PRISM", pre-release sanity checks, or after a big merge -- when there's no single ticket's AC list to walk (use qa-user-agent for that).
version: 1.0.0
---

# QA regression sweep -- whole-app health check, no specific ticket

## When to reach for this vs qa-user-agent

`qa-user-agent` walks ONE feature's stated ACs in depth. This sweep is
breadth-first: a fixed list of surfaces, checked shallowly but
consistently, to catch "something broke somewhere" after a merge, a
release, or a long session -- it is not a substitute for AC-level
validation of a specific change.

## Surfaces to crawl (confirm names via a live snapshot first -- nav
changes over time, don't trust this list blindly)

- Task list / board (app root)
- A task detail page with an open gate card (story/plan/red/green)
- The graph viewer (`/graph/viewer/{project}`)
- Workflows directory page
- Settings page

## Mechanism

This sweep needs console/network checks on EVERY surface, which
`agent-bridge-drive` cannot do yet (see `qa-user-agent`'s "Which
mechanism" section) -- default to Playwright MCP for this skill
specifically, even though `qa-user-agent` itself prefers agent-bridge.
Once console/network parity lands (epic 1d252db6), prefer the owner's
live tab here too.

## Per surface

1. Navigate via real nav, not a deep link.
2. `mcp__playwright__browser_snapshot` +
   `mcp__playwright__browser_take_screenshot`.
3. `mcp__playwright__browser_console_messages` +
   `mcp__playwright__browser_network_requests` -- flag anything new versus
   the last sweep, not just anything present.
4. One interaction core to that surface (open a task, expand a gate card,
   click a nav entry) -- a page that loads but whose one real action is
   broken is not healthy; a load-only check misses that entirely.

## Baselines

Store each sweep's screenshots plus a short pass/fail note under
`.claude/qa/baselines/<date>/` in this repo (small PNGs, commit them) so
the next sweep has something concrete on disk to diff against --
"compare to last time" needs an actual last time, not a memory of a prior
conversation.

## Reporting

List surface-by-surface pass/fail with the concrete thing you checked --
never a single "app looks fine." Anything red becomes a PRISM task
(`task_create`), never just narrated in chat and forgotten.

