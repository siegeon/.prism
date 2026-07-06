---
name: discover
description: Establish WHAT to build BEFORE prototyping — derive the actors, views, constraints, and user journeys, then render them as a shareable requirements Artifact that feeds /prototype. Use when the user says "discover", "requirements", "what should we build", "scope a feature", "who are the actors", "map the user journeys", "gather requirements", or is at the very start of a feature with no plan yet. Phase 1 of the PRISM SDLC arc (discover -> prototype -> implement -> verify -> ship -> operate); frontier-tier, model-agnostic. NOT for building — that's /prototype and /implement.
version: 1.0.0
---

# /discover — establish WHAT to build

The first phase of the PRISM lifecycle. Before a single mock or line of code,
`discover` fixes the REQUIREMENTS: who uses this, what surfaces they touch, the
constraints that bind it, and the journeys they walk. Its deliverable is a
**requirements Artifact** — a shareable page — that becomes `/prototype`'s input.

## Where it sits in the arc

`discover -> prototype -> implement -> verify -> ship -> operate` (the canonical
six, `models/workflow.py::LIFECYCLE`). Discover is **frontier-tier** (divergent
requirements reasoning — report the model you use). It has NO conductor state
machine backing today (status `missing`): it produces the artifact that the
conductor-tracked `/prototype` phase consumes. Do NOT claim a gate enforces it.

## When to use

- "discover" / "requirements" / "what should we build" / "scope a feature"
- "who are the actors" / "what are the user journeys" / "gather requirements"
- The user has a feature idea but no plan, no mock, no task yet.

**Do NOT use for:** building a clickable mock (that's [[prototype]]), working a
task through the conductor (that's [[implement]]), or proving quality after a
build (that's [[verify]]).

## Procedure

### 1. Mine PRISM first (never start from a blank page)

PRISM already knows most of the context. Load the tools and recall in parallel:

`ToolSearch("select:mcp__prism__brain_search,mcp__prism__memory_recall,mcp__prism__brain_understand")`

- `brain_search` (3-4 query variants) — existing surfaces, prior features,
  plumbing this feature would touch.
- `memory_recall` (3-4 variants) — conventions, past decisions, owner feedback
  that constrains the design (e.g. UI-first, progressive disclosure).
- Cite only what you actually read (CLAUDE.md: read before you reference).

### 2. Derive the four requirement axes

Interview the user where PRISM is silent; otherwise derive and confirm:

- **Actors** — who uses this (roles/personas), and what each one wants.
- **Views / surfaces** — the pages, panels, or API surfaces it manifests as.
  Every feature ships a UI surface (UI-FIRST is a PRISM rule) — name it here.
- **Constraints** — technical, business, and regulatory limits; existing PRISM
  conventions it must obey; non-goals.
- **Journeys** — the key end-to-end paths an actor walks, step by step, each
  ending in an observable outcome.

Be strict: a vague aspiration is not a requirement. Where evidence is thin,
record it as an OPEN QUESTION rather than asserting it.

### 3. Synthesize + render as an Artifact (the deliverable)

Structure the four axes into a requirements document, then RENDER it visually —
this is the phase's proof, and UI-first means it must be SEEN, not a JSON dump:

- Load the `artifact-design` skill, write the page, and publish it with the
  **Artifact** tool (a shareable requirements page).
- Render structured, never raw: sections for Actors, Views, Constraints,
  Journeys, and Open Questions — Hermes-styled, not `<pre>` blobs.
- Progressive disclosure for anything long (detailed journeys, constraint
  rationale): one-line summary + click-to-expand.

### 4. Hand off to /prototype

Discover's output is prototype's input. Close the phase by handing the
requirements artifact to [[prototype]] — its actors/views/constraints/journeys
map directly onto the `PLAN_SCHEMA` fields prototype synthesizes. State the
handoff explicitly: "requirements ready → run /prototype on <feature>".

## What "discovered" actually means

- ❌ "I asked some questions" — no captured artifact
- ❌ "here's a bullet list in chat" — not a rendered, shareable surface
- ✅ A published requirements **Artifact** covering actors / views / constraints
  / journeys (+ open questions), ready to feed `/prototype`.
