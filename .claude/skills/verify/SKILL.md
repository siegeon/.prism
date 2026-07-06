---
name: verify
description: Prove QUALITY across the whole feature, not just the implemented slice — run the quality MATRIX (full test suite, storybook/component states, a11y, docs, static analysis / lint / typecheck), collect evidence per axis, and render a pass/fail matrix that blocks /ship on any red. Use when the user says "verify", "quality check", "run the full matrix", "is this production-ready", "quality gate", "check a11y", "did we break anything", or has finished an implement slice and wants cross-cutting quality proof before release. Phase 4 of the PRISM SDLC arc (discover -> prototype -> implement -> verify -> ship -> operate); balanced-tier, model-agnostic.
version: 1.0.0
---

# /verify — the PRISM quality matrix

The cross-cutting quality phase. Where the conductor's `green_gate` proves the
ONE implemented slice is green, `verify` proves the WHOLE feature holds up —
across tests, component states, accessibility, docs, and static analysis. Its
deliverable is a **quality-matrix result**: each axis pass/fail with evidence.

> This is PRISM's PROJECT verify skill. The generic Claude Code `verify` skill
> bootstraps a per-project verify skill when none exists — this IS that skill
> for `.prism`. Invoke this one for the PRISM quality matrix.

## Where it sits in the arc

`discover -> prototype -> implement -> verify -> ship -> operate`
(`models/workflow.py::LIFECYCLE`). Verify is **balanced-tier**, runs AFTER
implement's `green_gate`, and is BROADER than that slice-check. Its honest
status today is `gates_only` — it lives as the conductor's green gates INSIDE
implement; this skill is the cross-cutting pass that composes with them. It
does not have its own state machine — don't claim a dedicated gate enforces it.

## When to use

- "verify" / "quality check" / "run the full matrix" / "quality gate"
- "is this production-ready" / "did we break anything else" / "check a11y"
- A build just cleared `green_gate` and you want quality proof before [[ship]].

**Do NOT use for:** driving a task's TDD cycle (that's [[implement]] +
green_gate) or the release itself (that's [[ship]], which calls verify as a
pre-flight gate). Verify PROVES; it does not release.

## The matrix — run every axis, collect evidence

Mine PRISM for the feature's real surfaces first
(`ToolSearch("select:mcp__prism__brain_search,mcp__prism__memory_recall")`),
then run each axis and capture concrete evidence (command + result, not a vibe):

- **Tests (full suite, not the slice)** — `cd services/prism-service && pytest
  tests/unit -q`. Zero failures; a red test is never "pre-existing" — it's a
  signal you broke something (CLAUDE.md). Evidence: pass count + any red name.
- **Typecheck** — `cd services/prism-service/prism_service/web && npx tsc -b`
  exits 0. Evidence: exit code.
- **Static analysis / lint** — the repo's configured linters. Evidence: clean
  or the specific findings.
- **Storybook / component states** — the changed components render in every
  state (empty / loading / error / populated); no dead empty states. Evidence:
  which stories/states, ideally a screenshot via [[agent-browser]].
- **A11y** — keyboard reachable, labelled controls, contrast holds in light AND
  dark (theme-aware). Evidence: what was checked and the result.
- **Docs** — user-visible change is documented where PRISM surfaces it
  (`PRISM_VERSION_NOTES` / relevant surface), not a stray README. Evidence: the
  entry.

Add feature-specific axes as needed (e.g. a customer-seat walkthrough for a UI
feature — works ≠ good; look at the screen).

## Render the matrix (progressive disclosure)

Structured, never raw: a one-line matrix summary (each axis ✅/❌) with each axis
EXPANDABLE to its evidence — never a wall of log text inline (a PRISM rule).
Surface it as an Artifact or a structured task-attached result, not `<pre>`.

## Verdict — any red axis is a ship-blocker

- **All axes green** → PASS. Verify is clear; proceed to [[ship]].
- **Any axis red** → BLOCKED. Name the failing axis + evidence; do NOT advance
  to ship. Fix the root cause (never ship around a red), then re-run the matrix.

## Composing with the conductor

`green_gate` (inside [[implement]]) checks the implemented slice; `verify` is the
cross-cutting quality pass over the whole feature. When driving through the
conductor, verify's matrix result is the evidence a `verify_green_state` /
`green_gate` approval should carry — the conductor task is the proof container.

## What "verified" actually means

- ❌ "tests pass" alone — that's one axis; tests-pass ≠ feature-works
- ❌ "green_gate approved" — that's the slice, not the matrix
- ✅ A rendered quality matrix, every axis green with cited evidence, cleared
  to hand to /ship — or a named red axis blocking it.
