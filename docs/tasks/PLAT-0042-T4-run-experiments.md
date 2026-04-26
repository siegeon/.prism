# Task PLAT-0042-T4: Run baseline + decomposition smokes

## Story Reference
- **Parent Story:** PLAT-0042 — Query decomposition for Brain candidate generation
- **Story Link:** [PLAT-0042](../stories/PLAT-0042-retrieval-query-decomposition.story.md)

## Task Description

Run the LongMemEval smoke (N=50, fixed seed) twice — once on the
current baseline, once with `PRISM_QUERY_DECOMP=on` — and append the
result row to `benchmarks/results/EXPERIMENTS.md`. Capture R@5,
pool_recall@50 (from T3), and median per-query Brain latency.

## Acceptance Criteria Coverage
- AC-4: smoke recall lift, candidate-side, ≥+0.02 R@5, ≥+2 sessions in pool.
- AC-5: median latency ≤ 1.6× baseline.

## Implementation Requirements

### Functional
1. Run baseline smoke; record R@5, pool_recall@50, median latency.
2. Restart bench service with `PRISM_QUERY_DECOMP=on`; run smoke with the same seed.
3. Verify ΔR@5 ≥ +0.02 and ≥2 additional gold sessions enter the top-50 pool.
4. Verify median latency ratio ≤ 1.6.
5. Append a row to `benchmarks/results/EXPERIMENTS.md` describing the change, smoke R@5, full status, and notes.

### Technical
- Use `benchmarks/run_experiments.py` orchestration pattern (env-var swap, compose down/up).
- **Files modified:** `benchmarks/results/EXPERIMENTS.md` (append a row).

## Dependencies
- **Upstream:** PLAT-0042-T2, PLAT-0042-T3
- **Downstream:** PLAT-0042-T5

## Testing Requirements

### Manual
- Both smoke runs complete without errors; gold-pool delta logged.
- If ΔR@5 < +0.02, do NOT call AC-4 met — escalate (HyDE follow-on may be needed).

### Definition of Done
- [ ] Baseline + decomp rows present in EXPERIMENTS.md
- [ ] AC-4 thresholds met OR escalation note added with the gap
- [ ] AC-5 latency ratio recorded

## Estimation
- **Estimated Hours:** 4 / 6 / 10
- **Size:** small
- **Confidence:** medium (smoke runtime + container restarts dominate)

**Status:** Not Started
**Created:** 2026-04-24
