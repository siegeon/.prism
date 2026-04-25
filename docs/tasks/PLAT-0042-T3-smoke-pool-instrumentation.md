# Task PLAT-0042-T3: Smoke harness — record `gold_in_pool@50`

## Story Reference
- **Parent Story:** PLAT-0042 — Query decomposition for Brain candidate generation
- **Story Link:** [PLAT-0042](../stories/PLAT-0042-retrieval-query-decomposition.story.md)

## Task Description

Extend `benchmarks/longmemeval/run.py` to record, per query, whether
the gold session entered the top-50 RRF candidate pool (recall@50).
This separates "candidate generation lifted" from "rerank got lucky"
and is the instrumentation needed to validate AC-4 honestly.

## Acceptance Criteria Coverage
- AC-4 (instrumentation half): proves the lift comes from candidate generation.

## Implementation Requirements

### Functional
1. For each smoke item, additionally request the top-50 pool from `brain_search` (not just top-K).
2. Record `gold_in_pool@50` boolean per item.
3. Surface aggregate `pool_recall@50` in the printed summary line and the result JSON.

### Technical
- **Files modified:** `benchmarks/longmemeval/run.py`
- Use the existing `mcp_call(... brain_search ... limit=50)` shape; do not require a new server endpoint.

## Dependencies
- **Upstream:** none (parallelizable with T1/T2)
- **Downstream:** PLAT-0042-T4

## Testing Requirements

### Unit Tests
- New `benchmarks/longmemeval/tests/test_pool_recall.py`: monkeypatch `mcp_call` to return a fixed top-50 list; assert `pool_recall@50` is computed correctly and written to the result JSON under the documented key.
- Edge case: gold session not present in the pool → contributes 0 to the aggregate.

### Manual
- Run smoke against current baseline; confirm `pool_recall@50` ≈ R@50 from EXPERIMENTS.md history (sanity check).

### Definition of Done
- [ ] Automated test asserts `pool_recall@50` in result JSON
- [ ] `pool_recall@50` printed in summary line
- [ ] Baseline smoke run reproduces previous R@5 within seed noise

## Estimation
- **Estimated Hours:** 2 / 3 / 5
- **Size:** very small
- **Confidence:** high

**Status:** Not Started
**Created:** 2026-04-24
