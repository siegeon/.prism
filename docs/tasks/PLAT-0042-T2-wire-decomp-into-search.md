# Task PLAT-0042-T2: Wire decomposition into `Brain.search`

## Story Reference
- **Parent Story:** PLAT-0042 — Query decomposition for Brain candidate generation
- **Story Link:** [PLAT-0042](../stories/PLAT-0042-retrieval-query-decomposition.story.md)

## Task Description

Integrate `decompose_query` from T1 into `Brain.search`
(`services/prism-service/app/engines/brain_engine.py:2355`). Behind
`PRISM_QUERY_DECOMP` env var. When enabled, run each sub-query
through the existing per-index helpers, union per index by best rank,
then call `reciprocal_rank_fusion` once across the unioned per-index
lists. When disabled, the existing single-query path runs unchanged.

## Acceptance Criteria Coverage
- AC-2: Brain.search fuses sub-query candidate pools.
- AC-3: Off-by-default and byte-identical when disabled.

## Implementation Requirements

### Functional
1. Read `PRISM_QUERY_DECOMP` env var (mirror `PRISM_SEARCH_MODE` pattern already in this method).
2. When on: call `decompose_query(query)`, run each sub-query through `_fts5_search`, `_vector_search`, `_graph_search`; union per index keyed on `doc_id` taking the best (lowest) rank per id; then `reciprocal_rank_fusion` once.
3. When off, "0", or unset: call sequence unchanged. Regression-guarded.
4. Apply rerank, feedback, aggregation steps **after** the fused stage exactly as today.

### Technical
- **Files modified:** `services/prism-service/app/engines/brain_engine.py` (around line 2400-2415).

## Dependencies
- **Upstream:** PLAT-0042-T1
- **Downstream:** PLAT-0042-T3, PLAT-0042-T4

## Testing Requirements

### Unit Tests
- New test seam: monkeypatch `decompose_query` to return 2 fixed sub-queries; assert per-index helpers called twice each; assert RRF input shape.
- Regression test: with env unset, `Brain.search(q)` output is byte-identical to a recorded baseline on a fixed seed.

### Definition of Done
- [ ] Env-var gating works both ways
- [ ] AC-3 byte-identical regression test passes
- [ ] Existing brain_engine tests still green

## Estimation
- **Estimated Hours:** 5 / 7 / 10
- **Size:** medium
- **Confidence:** medium (touching hot search path; regression risk)

**Status:** Not Started
**Created:** 2026-04-24
