---
name: qa-visual-diff
description: Compare PRISM's rendered UI against an approved plan_gate prototype/design mock, explicitly and surface-by-surface -- never "looks close." Use when validating a UI change against its approved prototype, or whenever asked "does this match the design/mock." Called from qa-user-agent's visual-comparison step, or standalone.
version: 1.0.0
---

# QA visual diff -- compare against the approved design, explicitly

## Why explicit, not eyeballed

A build can pass every test and gate while showing none of what the owner
actually approved -- a prototype only gets checked once, at plan_gate, and
nothing forces anyone to re-open it once implementation starts. The fix is
a named checklist, not a vibe.

## Steps

1. Fetch the approved artifact: `GET /api/tasks/<id>/prototype?project=<proj>`
   -- only if `has_prototype` is true on the task. Note this 404s without
   the `project` query param; that is not "no prototype exists."
2. Drive the REAL running feature to the equivalent state -- default to
   `agent-bridge-drive` (the owner's real tab), falling back to Playwright
   MCP only per `qa-user-agent`'s "Which mechanism drives the browser"
   (see that skill for entry-point/navigation discipline -- reach it via
   real nav, not a deep link).
3. Screenshot the real feature at that state (`browser_take_screenshot`,
   or agent-bridge's `screenshot`).
4. Build a surface-by-surface checklist FROM the prototype -- every
   distinct element, section, nav entry, button, and label the mock shows
   -- then mark each one present / absent / different in the real
   screenshot. A single "looks about right" verdict is not acceptable
   output here; the checklist IS the deliverable.
5. Save both images (prototype render + real screenshot) into the task's
   evidence store; the checklist text is part of the proof, not a chat-only
   aside.
6. If there's no task, or no prototype to fetch, say so explicitly instead
   of presenting an unanchored comparison -- "matches my expectations" with
   no artifact behind it is not a visual diff, it's an opinion.

## Common ways this goes wrong

- Comparing against `plan_doc` prose instead of the actual prototype bytes
  -- prose drifts from the artifact the owner actually approved.
- Confirming the feature was BUILT without confirming it appears where and
  how the mock showed it (same nav entry, same card position, same
  surface) -- a relocated or renamed feature reads as a redesign nobody
  asked for.
- Skipping the fetch when a prototype exists but is inconvenient to reach
  -- the missing `?project=` param is the usual excuse, not a real 404.

