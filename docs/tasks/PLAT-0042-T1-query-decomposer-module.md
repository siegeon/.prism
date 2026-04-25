# Task PLAT-0042-T1: Build `query_decomposer` module

## Story Reference
- **Parent Story:** PLAT-0042 — Query decomposition for Brain candidate generation
- **Story Link:** [PLAT-0042](../stories/PLAT-0042-retrieval-query-decomposition.story.md)

## Task Description

Create a pure-Python, rules-based query decomposer at
`services/prism-service/app/engines/query_decomposer.py`. No LLM hop.
The function splits compound questions into atomic sub-queries while
always preserving the raw query as a fallback.

## Acceptance Criteria Coverage
- AC-1: Decomposer produces sub-queries (raw query always included; trivial inputs return `[q]` only).

## Implementation Requirements

### Functional
1. Public function: `decompose_query(q: str, max_subs: int = 4) -> list[str]`
2. Trigger condition: decompose only when `q` contains " and ", " then ", or `;`, OR exceeds **12 tokens** (whitespace-split). Otherwise return `[q]` unchanged. This matches AC-1's compound-query definition exactly.
3. Split rules: " and ", " then ", semicolons; strip filler ("what was", "tell me", "do you know"); drop fragments under 3 tokens.
4. Always include the raw `q` in the output. Dedupe case-insensitively. Cap at `max_subs`.
5. Trivial inputs (≤6 tokens AND no connective) return `[q]` only.

### Technical
- **New file:** `services/prism-service/app/engines/query_decomposer.py`
- **New file:** `services/prism-service/tests/engines/test_query_decomposer.py`

## Dependencies
- **Upstream:** none
- **Downstream:** PLAT-0042-T2 (wires this into Brain.search)

## Testing Requirements

### Unit Tests
- Trivial query ("auth bug") → `["auth bug"]`
- Compound ("how did Bob fix the auth bug and what was the migration plan") → ≥2 sub-queries
- Edge cases: empty string, single word, all-stopwords, 50-token query, embedded semicolons

### Definition of Done
- [ ] Module + tests committed on branch `PLAT-0042-query-decomposition`
- [ ] `python -m pytest services/prism-service/tests/engines/test_query_decomposer.py` green
- [ ] AC-1 validated by tests

## Estimation
- **Estimated Hours:** 4 / 6 / 8 (PROBE optimistic / likely / pessimistic)
- **Size:** small
- **Confidence:** high (no I/O, no external dep)

**Status:** Not Started
**Created:** 2026-04-24
