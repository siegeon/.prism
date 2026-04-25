# Task PLAT-0042-T5: Document `PRISM_QUERY_DECOMP` env var

## Story Reference
- **Parent Story:** PLAT-0042 — Query decomposition for Brain candidate generation
- **Story Link:** [PLAT-0042](../stories/PLAT-0042-retrieval-query-decomposition.story.md)

## Task Description

Add `PRISM_QUERY_DECOMP` to the Brain operator documentation: name,
default, accepted values, expected effect on recall and latency, and
when an operator might disable it.

## Acceptance Criteria Coverage
- Operational requirement (no AC). Story-level concern: operators must be able to find and toggle the gate. Not a substitute for AC-3 (regression guard, proven by T2 byte-identity test).

## Implementation Requirements

### Functional
1. Update the docstring of `Brain.search` in `services/prism-service/app/engines/brain_engine.py` (line 2355) to list `PRISM_QUERY_DECOMP` alongside the existing `PRISM_SEARCH_MODE`, `PRISM_CHUNK_AGG`, `PRISM_RERANK`, `PRISM_FEEDBACK_WEIGHT` paragraphs — this is the project's de-facto env-var doc surface.
2. Append a one-line entry to `benchmarks/EXPERIMENTS.md` env-var legend with: default `off`, accepted values `on|off|0|1`, measured R@5 lift from T4, measured median-latency multiplier, operator guidance ("disable if latency-sensitive").

### Technical
- **Files modified:**
  - `services/prism-service/app/engines/brain_engine.py` (docstring on `Brain.search`)
  - `benchmarks/EXPERIMENTS.md` (env-var legend section)

## Dependencies
- **Upstream:** PLAT-0042-T4 (need real numbers for the doc)
- **Downstream:** none

## Testing Requirements

### Manual
- Doc renders, env-var name matches code, numbers cited match T4's EXPERIMENTS.md row.

### Definition of Done
- [ ] Env-var documented with measured numbers
- [ ] Default and toggle semantics stated

## Estimation
- **Estimated Hours:** 1 / 2 / 3
- **Size:** very small
- **Confidence:** high

**Status:** Not Started
**Created:** 2026-04-24
