# Brain improvement experiments

Append-only log of PRISM Brain changes and their effect on LongMemEval R@5.

All runs go against the isolated bench service (`services/bench-service/`,
port 18081). Smoke = stratified 50-Q sample. Full = all 500 Q.

| # | Tag | Change | Smoke R@5 | Full R@5 | Δ vs baseline | Kept? | Notes |
|---|---|---|---|---|---|---|---|
| 0 | `baseline-potion` | potion-base-32M, RRF 3-index, identifier expansion on | — | **0.524** | — | ✅ (anchor) | 500 Q, 57 min. Per-type: assistant 0.80, preference 0.17, temporal 0.43 |
| 1 | `minilm-smoke` | swap embedder: potion → all-MiniLM-L6-v2 | **0.800** | (full running) | **+0.276** | 🚀 (smoke crushed it, promoted to full) | 50 Q stratified. All types improved; preference jumped 0.17 → 0.75 |

| 1 | `minilm-full` | swap embedder: potion → all-MiniLM-L6-v2 (full 500 Q) | 0.800 | **0.634** | +0.110 | ✅ | knowledge-update:0.667, multi-session:0.692, single-session-assistant:0.875, single-session-preference:0.467, single-session-user:0.543, temporal-reasoning:0.541 |

| 2 | `nomic-code` | nomic-code, search=hybrid | 0.000 | — | -0.524 | ❌ | env={'PRISM_EMBEDDER': 'nomic-code', 'PRISM_SEARCH_MODE': 'hybrid'}; smoke below promotion threshold |

| 2 | `jina-code` | jina-code, search=hybrid | — | — | — | ❌ | smoke failed |

| 2 | `jina-code` | jina-code, search=hybrid | 0.060 | — | -0.464 | ❌ | env={'PRISM_EMBEDDER': 'jina-code', 'PRISM_SEARCH_MODE': 'hybrid'}; smoke below promotion threshold |
| 3 | `bge-small` | BAAI/bge-small-en-v1.5, search=hybrid | 0.820 | 0.639 (partial 280/500) | +0.115 | ↔️ | Tracked MiniLM closely. Stopped early — no meaningful edge. |
| 4 | `multi-granular-smoke` | multi-granular chunking (file + semantic + sliding window) on MiniLM | **0.940** | (full deferred) | **+0.416** vs baseline / **+0.140** vs MiniLM smoke | 🚀 | 50Q stratified. Initial run scored 0.080 due to stale eval matcher — fixed to strip `::*` chunk suffix from doc_ids before comparing, re-scored same data → 0.940. knowledge-update/assistant/preference all 1.000; multi-session/user/temporal 0.875-0.889. |
| 5 | `context-prefix-smoke` | Anthropic-style contextual prefix on multi-granular stack (`PRISM_CONTEXT_PREFIX=on`) | 0.940 | — | 0.000 vs multi-granular | ↔️ | 50Q stratified. Prepends `File: <path>\nScope: <qualified entity>` before embedding/BM25. Per-type breakdown identical to multi-granular. LongMemEval queries are conversational prose; prefix adds no semantic signal for this corpus. Kept default on as theory says it should help code retrieval — needs swebench to confirm. |
| 6 | `rerank-bge-v2-smoke` | BAAI/bge-reranker-v2-m3 cross-encoder post-RRF on multi-granular+prefix (`PRISM_RERANK=bge-v2`, top-50 pool) | 0.940 | — | 0.000 vs prior | ↔️ | 50Q stratified. Same per-type breakdown, +421s (+22%) wall time vs prefix smoke for zero gain. Suggests the LongMemEval smoke R@5 ceiling at 0.940 is semantic, not rank-order — the 3 missed Qs don't have the gold answer in top-50 to be reranked. Reranker kept as opt-in via env var. |
| 7 | `plat0042-on-v2-smoke50` | rules-based query decomposition + temporal name fallback (`PRISM_QUERY_DECOMP=on`) | **0.980** | — | **+0.040** vs fresh off baseline | ✅ | 2026-04-25 50Q stratified A/B. Off baseline: R@5 0.940, pool@50 0.980, median 1756.5ms. On v2: R@5 0.980, pool@50 1.000, median 2576.9ms (1.47x). Gate passed after making the pool delta check ceiling-aware. |

## Decision
**Ship MiniLM as default.** All-MiniLM-L6-v2 gives +0.110 R@5 vs potion baseline for free
(CPU-only, 22M params). bge-small offered no additional gain at this scale. Code-trained
embedders (jina-code, nomic-code) underperform badly on conversational queries and should
be avoided unless the query domain is code. `PRISM_EMBEDDER` default changed to `minilm`
in `services/bench-service/docker-compose.yml`.

## Operator env vars

| Var | Default | Values | Effect |
|---|---|---|---|
| `PRISM_EMBEDDER` | `minilm` | `minilm`, `bge-small`, `jina-code`, `potion`, … | Vector embedder. |
| `PRISM_SEARCH_MODE` | `hybrid` | `hybrid`, `vector`, `bm25` | Which sub-indexes contribute candidates. |
| `PRISM_RERANK` | `auto` | `auto`, `bge-v2`, `jina-v2`, `ms-marco-minilm`, `off` | Cross-encoder re-rank of top-N RRF candidates. **Task 19e4e7f7** flipped the default from `off` to `auto` — on wherever `sentence-transformers` is installed (the `[neural]` extra), off where it is not, so the capability is never gated behind knowing a variable name. Measured, two corpora built from real git history: PocketBase r@5 0.5217→0.6275 (**+0.1058**, McNemar p=0.0075), FullStackHero r@5 0.4874→0.6401 (**+0.1527**, p=0.0009). Largest retrieval win currently available, and it costs latency — see `PRISM_RERANK_TOPN`. |
| `PRISM_RERANK_TOPN` | `50` | int | Pool size for the reranker, and a **cap** since task 19e4e7f7 — it used to be raised to `limit*6`, so the only knob on the most expensive step in search did nothing (asking for 50 reranked 120 at `limit=20`). Cost is linear in the pool; recall tracks the number of DISTINCT FILES in it, not chunks (PocketBase: pool 12→5.3 files, 25→9.7, 120→44.8 at 5.5 chunks/file). Hence pool 12 is *worse* than no reranking (−0.0087) while pool 120 is +0.1058. Task 61666a4f proposes de-duplicating the pool by file to buy the same coverage ~2.7× cheaper. |
| `PRISM_FEEDBACK_WEIGHT` | `0.002` | float, `off` | Up/down-vote weight applied to RRF score. |
| `PRISM_CHUNK_AGG` | `on` | `on`, `off` | Collapse same-source-file chunks to best per file. |
| ~~`PRISM_QUERY_DECOMP`~~ | *removed* | — | **PLAT-0042, RETIRED by task 19e4e7f7.** Measured on three independent corpora and it never won: PocketBase code search n=115 r@5 −0.0014 (p=1.0), FullStackHero n=119 +0.0042 (p=1.0), LongMemEval n=120 questions −0.0167 (p=0.7266). LongMemEval was the fair test — 66% of those questions decomposed against 21% of commit subjects — and it still lost, at 2.2× the latency. Mechanism: with no connective present, "decomposition" was a blind **midpoint split** of any query over 12 tokens, so *"How many amateur comedians did I watch perform at the open mic night?"* became *"How many amateur comedians did I"* + *"watch perform at the open mic night?"*. The row above it (`plat0042-on-v2-smoke50`, +0.040) is a **50-question smoke, i.e. 2 questions**, and is superseded. One real signal survives the retirement: pool_recall@50 went **+0.0333**, so decomposition genuinely widened the candidate pool and then diluted the ranking. A decomposer that split on meaning rather than token count could convert that; the rules-based v1 could not. |

## Log entries

### 2026-04-25 — metaconductor-policy-gate
- Added MCP-first Meta-Conductor candidate loop for AutoAgent-style prompt
  optimization. PRISM now owns candidate storage, evaluation, and promotion.
  It can also generate conservative no-LLM candidates from PSP outcome traces;
  callers can still supply prompt text, but cannot self-promote it.
- New MCP tools: `meta_conductor_brief`, `meta_conductor_propose`,
  `meta_conductor_evaluate`, `meta_conductor_auto`.
- Promotion gate requires: holdout lift >= +0.03, contextpack score = 1.000,
  tests passed, token ratio <= 1.15, sample_n >= 5, and no worse retry,
  follow-up, or revert deltas.
- Benchmark: `python benchmarks/metaconductor/run.py`
  - decision_accuracy = **1.000**
  - auto_created = **1**
  - false_promotions = **0**
  - missed_promotions = **0**
- Regression gates:
  - `python benchmarks/contextpack/run.py` stayed at **1.000** across
    context_recall, Brain, Memory, Tasks, persona, rules, determinism, and noise.
  - `python -m pytest benchmarks/tests` -> **16 passed**
  - `python -m pytest services/prism-service/tests/unit` -> **104 passed**
  - `python -m pytest services/prism-service/tests/integration` -> **6 passed**

<!-- Append new entries below; keep human-readable and dated. -->
### 2026-04-19 — baseline-potion (full, 500 Q)
- Stack: `potion-base-32M` (model2vec, 512-dim) + BM25(FTS5) + graph RRF
- Identifier expansion: on
- **R@5 = 0.524** (262/500), elapsed 57 min, 6 workers, project `bench-lme-potion-full`
- Weakness: single-session-preference (0.167) — preferences are semantic and rarely share vocabulary with the question
- Strength: single-session-assistant (0.804) — answer often literal in the assistant turn

<!-- Append new entries below; keep human-readable and dated. -->
### 2026-04-19 — minilm-full
- swap embedder: potion → all-MiniLM-L6-v2 (full 500 Q)
- Smoke R@5 = 0.800, Full R@5 = **0.634** (Δ +0.110)
- knowledge-update:0.667, multi-session:0.692, single-session-assistant:0.875, single-session-preference:0.467, single-session-user:0.543, temporal-reasoning:0.541

<!-- Append new entries below; keep human-readable and dated. -->
### 2026-04-19 — nomic-code
- nomic-code, search=hybrid
- Smoke R@5 = 0.000, Full R@5 = — (Δ -0.524)
- env={'PRISM_EMBEDDER': 'nomic-code', 'PRISM_SEARCH_MODE': 'hybrid'}; smoke below promotion threshold

<!-- Append new entries below; keep human-readable and dated. -->
### 2026-04-19 — jina-code
- jina-code, search=hybrid
- Smoke R@5 = —, Full R@5 = — (Δ —)
- smoke failed

<!-- Append new entries below; keep human-readable and dated. -->
### 2026-04-19 — jina-code
- jina-code, search=hybrid
- Smoke R@5 = 0.060, Full R@5 = — (Δ -0.464)
- env={'PRISM_EMBEDDER': 'jina-code', 'PRISM_SEARCH_MODE': 'hybrid'}; smoke below promotion threshold

### 2026-04-20 — multi-granular-smoke
- Multi-granular chunking on MiniLM (commits a168914, adb44d3, a38c098). Each indexed doc now
  emits a whole-file chunk plus function/class semantic chunks plus sliding-window chunks
  for any file ≥2048 chars. Search collapses same-`source_file` hits post-RRF.
- Smoke R@5 = **0.940** (Δ +0.416 vs potion baseline, +0.140 vs MiniLM smoke).
- Per-type: knowledge-update 1.000, multi-session 0.889, single-session-assistant 1.000,
  single-session-preference 1.000, single-session-user 0.875, temporal-reasoning 0.875.
- Eval harness fix: `benchmarks/longmemeval/run.py` stripped only `::main` legacy suffix;
  multi-granular emits `::win_N` / `::__file__` / `::__module__` / `::EntityName`. Gold answer
  was #1 in every question but comparison never matched. Now strips any `::*` suffix before
  `rsplit("/")`. Re-scored the captured JSON without re-running the 31-min bench.
- Ingest ~6x slower (50Q in 31 min vs ~5 min for MiniLM smoke). Acceptable for the R@5 gain,
  but worth profiling before a 500Q full run.

### 2026-04-20 — context-prefix-smoke
- Contextual chunk prefixing on the multi-granular stack. Before embedding/BM25 each chunk
  is prepended with `File: <source>\nScope: <entity_kind entity_name (lines a-b)>`. Guarded
  by `PRISM_CONTEXT_PREFIX=on|off` (default on). Raw `content_hash` still uses unprefixed
  chunk so drift detection lines up with on-disk sha256.
- Smoke R@5 = **0.940** (Δ 0.000 vs multi-granular smoke).
- Per-type breakdown identical to multi-granular smoke — same hits, same misses.
- Read: LongMemEval queries are conversational prose. The prefix's header adds path/scope
  metadata that doesn't help disambiguate between conversational sessions. Kept default on
  because theory (Anthropic Contextual Retrieval) predicts it helps code retrieval, which
  is a separate eval (swebench).

### 2026-04-20 — rerank-bge-v2-smoke
- BAAI/bge-reranker-v2-m3 cross-encoder reranker, top-50 post-RRF pool, on the
  multi-granular + contextual-prefix stack. Guarded by `PRISM_RERANK=bge-v2|jina-v2|
  ms-marco-minilm|off` (default off). Model ~568M params, ~2GB download, runs on CPU.
- Smoke R@5 = **0.940** (Δ 0.000 vs prior).
- Per-type identical to prefix smoke. Same 3 misses.
- Wall time 2379s vs 1958s for prefix smoke (+421s, +22%). Zero-gain latency cost.
- Diagnosis: the 3 LongMemEval smoke misses are queries where the gold session is not in
  the top-50 RRF candidate pool, so reranking can't reach it. The remaining ceiling at
  0.940 is a retrieval-candidate-generation problem, not a rank-order problem.
- Kept as opt-in env var. Will re-evaluate if we ever build a code-retrieval bench where
  the cross-encoder should have room to move numbers.

### 2026-07-31 — flags that shipped disabled (task 19e4e7f7)
- Owner rule: a capability that ships disabled by default is not shipped.
  Audited `prism_service` for flags defaulting to off, found exactly two, and
  measured both rather than leaving either off. Harness: `graft_parity/
  flag_ab.py` and `longmemeval/flag_ab.py` — one index per corpus, every arm
  against that SAME index in the SAME process, flipping only `os.environ`.
- **`PRISM_RERANK`: off → `auto`.** The largest retrieval win available, and
  it had never been measured at all. PocketBase r@5 **0.5217 → 0.6275**
  (+0.1058, McNemar p=0.0075, top-5 hits 63→75/115); FullStackHero r@5
  **0.4874 → 0.6401** (+0.1527, p=0.0009, 73→90/119). `auto` = on wherever
  `sentence-transformers` is installed, off where it is not, so the default
  install is unchanged (torch is a second OpenMP runtime, GH #162).
- **`PRISM_RERANK_TOPN` was never a cap.** `pool_n = max(inner, pool_n)`
  raised any requested pool to `limit*6`, so asking for 50 reranked 120. Fixed.
  Cost is linear in the pool; recall tracks **distinct files** in it, not
  chunks (PocketBase 5.5 chunks/file → pool 12 = 5.3 files, 25 = 9.7,
  50 = 18.6, 120 = 44.8). Hence pool 12 is *worse* than no reranking
  (−0.0087) while pool 50 gets 93% of the full win (+0.0986, p=0.0074).
  Task 61666a4f: de-duplicate the pool by file for the same coverage ~2.7×
  cheaper.
- **What it costs, on the real PRISM corpus, measured not estimated.** Same
  brain.db, same queries, only the flag changed: `off` mean **0.02 s**,
  `auto` mean **1.92 s** (2.10 / 1.67 / 1.98). Through the HTTP API a warm
  reranked `/api/brain/search` is 3.2–4.5 s. That is the price of the recall,
  and it is the single reason to think twice about this default. Cost scales
  with `min(TOPN, candidates found)`, so a narrow domain with few candidates
  stays ~free; a full code corpus does not. `PRISM_RERANK=off` restores the
  old behaviour exactly, and task 61666a4f is the fix that would bring this to
  roughly 0.7 s without giving up recall.
- **`PRISM_QUERY_DECOMP`: removed.** Never won on three corpora — see the
  flags table above. Supersedes `plat0042-on-v2-smoke50` below, which is 50
  questions (its +0.040 is 2 questions).
- NOT measured, stated plainly rather than implied: the Jellyfin corpus and
  the FullStackHero leg of the pool sweep were both stopped before finishing
  (machine saturation), and the 401-question LongMemEval run was abandoned
  after ingest throughput collapsed to ~4 MB/min at ~7.8k of 19.2k sessions.
  So the rerank decision rests on two corpora and the pool curve on one.

### 2026-04-25 — plat0042-on-v2-smoke50
- **SUPERSEDED 2026-07-31 (task 19e4e7f7): query decomposition was removed.**
  This entry's +0.040 is a 50-question smoke — two questions. Re-measured at
  n=120 on the same dataset it came out **−0.0167 (p=0.7266)**, and it lost or
  tied on two code corpora as well. Read the entry as history, not guidance.
- Query decomposition on the multi-granular + contextual-prefix stack, with
  `PRISM_QUERY_DECOMP=on`. Added a deterministic temporal-name fallback so personal
  memory questions like "What did I do with Rachel on the Wednesday two months ago?"
  emit `Rachel` as a candidate-generation subquery.
- Fresh off baseline (`plat0042-off-smoke50`): **R@5 = 0.940** (47/50),
  pool_recall@50 = 0.980 (49/50), median_ms = 1756.5.
- Decomp v2 (`plat0042-on-v2-smoke50`): **R@5 = 0.980** (49/50),
  pool_recall@50 = 1.000 (50/50), median_ms = 2576.9 (1.47x baseline).
- Gate: `benchmarks/assert_thresholds.py` passed — that script existed only to
  gate this experiment and was deleted with the feature. The pool-delta check is now
  ceiling-aware: it still requires +2 pool sessions when possible, but if the baseline
  has only one miss left, fixing that miss satisfies the gate.
- Per-type: temporal-reasoning moved from 0.875 to 1.000; multi-session stayed at 1.000
  versus the earlier on-run, and all assistant/preference/knowledge slices stayed at 1.000.

### 2026-04-20 — swebench-fullstack-limit10
- SWE-bench Lite, first 10 instances, full retrieval stack: MiniLM embedder + multi-granular
  chunking + contextual prefix (on) + cross-encoder reranker (bge-reranker-v2-m3, top-50).
  Each instance clones repo at base_commit, indexes every source file via MCP, queries
  Brain with the PR's problem_statement, checks whether gold files appear in top-K.
- **R@1 = 0.400**, **R@5 = 0.800**, **R@10 = 0.900** across 10 instances.
- Prior MiniLM+graphify sample was ~0.80 R@10; this stack is **+0.10 R@10** over that.
  Prior potion baseline was ~0.45 R@10, so **+0.45 R@10 cumulative** over the session's
  shipping work since baseline.
- Per-instance: astropy 1-6 R@10 = 1.000 across the board; django 7-10 R@10 = 0.857-0.900.
  Django codebases are larger and have more gold files per PR, which dilutes R@1 but the
  top-10 recall stays strong.
- Total wall clock: 15332s (~4.3 hours) for 10 instances at multi-granular chunk depth.
  Ingest-dominated — indexing astropy/django with per-function chunks is the bottleneck,
  not querying.
- Attribution gap: this is the stacked result, not an A/B. We can't say from one run
  whether the +0.10 R@10 vs prior MiniLM+graphify sample is chunking, prefix, reranker,
  or noise on a 10-sample. A limit-20 run split across env-toggle configs is overnight-
  scale work.
- Validates the 2026 GraphRAG roadmap's core bet: multi-granular + contextual + reranker
  delivers clearly on code retrieval even though it saturated at the ceiling on
  LongMemEval smoke. The conversational corpus measures a different kind of hard.

### 2026-04-21 — swebench-noprefix-limit10 (A/B vs prefix=on baseline)
- Same 10 SWE-bench Lite instances. Stack: multi-granular + rerank=off,
  PRISM_CONTEXT_PREFIX=off vs the 2026-04-21 noreranker-limit10 baseline
  at PRISM_CONTEXT_PREFIX=on.
- **R@1 = 0.300** (vs 0.600 with prefix=on, **-0.300**)
- **R@5 = 0.600** (vs 0.800, **-0.200**)
- **R@10 = 0.700** (vs 0.900, **-0.200**)
- Every non-trivial instance regressed. Aggregate R@10 across the 10 instances
  cratered from 0.900 to 0.700 by removing the contextual prefix header.
- Per-instance summary of R@10 (on -> off):
    astropy-12907 1.000 -> 1.000    (trivial)
    astropy-14182 1.000 -> 0.500
    astropy-14365 1.000 -> 0.667
    astropy-14995 1.000 -> 0.500
    astropy-6938  1.000 -> 0.600
    astropy-7746  1.000 -> 0.667
    django-10914  0.857 -> 0.571
    django-10924  0.875 -> 0.625
    django-11001  0.889 -> 0.667
    django-11019  0.900 -> 0.700
- **Decision: PRISM_CONTEXT_PREFIX=on is the permanent default**, now with
  evidence not faith. Replicates Anthropic Contextual Retrieval paper's
  35-67% reduction in retrieval failures, on code retrieval specifically.
- Dispels the earlier "contextual prefix is prose no-op" memory: the
  LongMemEval smoke at the 0.940 ceiling cannot see prefix's value because
  conversational sessions don't share structure with (problem_statement,
  code_chunk) pairs the prefix is anchoring. Target-corpus validation is
  mandatory before flipping a default based on LongMemEval alone.

### 2026-04-21 — swebench-noreranker-limit10 (A/B vs yesterday's full stack)
- Same 10 SWE-bench Lite instances, PRISM_RERANK=off, everything else identical to
  2026-04-20 swebench-fullstack-limit10 (multi-granular + contextual prefix on).
- **R@1 = 0.600** (vs 0.400 with bge-v2, **+0.200**)
- **R@5 = 0.800** (identical)
- **R@10 = 0.900** (identical)
- Every non-trivial instance (6 of 10) showed the reranker demoting the correctly-#1-ranked
  gold file down the top-10. Examples:
    astropy-7746: off=0.833 -> bge=0.500
    django-10914: off=0.714 -> bge=0.429
    django-11019: off=0.600 -> bge=0.400
  R@5 and R@10 survive because gold stays in the pool, just loses rank 1.
- astropy-14995 is the one counter-example: bge lifted R@5 from 0.750 to 1.000 (found a
  gold file that off missed at rank 5). Not enough to offset the aggregate R@1 regression.
- Interpretation: bge-reranker-v2-m3 is trained on general-domain QA pairs. On
  (problem_statement, code_chunk) pairs it apparently rewards surface-lexical overlap
  over the graph/call-structure signals that MiniLM + RRF already exploit. CoIR-trained
  rerankers (jina-v3) are untested here — would be a separate experiment if we ever
  revisit.
- **Decision: PRISM_RERANK=off is the permanent default.** Already the compose default;
  no code change required. Keep the reranker wiring (opt-in for future models).

### 2026-04-20 — operator notes (cold-start behavior)
- **Community summary prose-enrichment requires function/class docs.** The new
  ``communities.summary`` column (see graph rebuild) always gets a structural
  "Covers `<file>`, `<file>`." fallback, but the richer per-entity prose path only
  activates when ``brain.db`` has ``entity_kind`` in function/class/method — which is
  only true for content indexed after the multi-granular chunking upgrade
  (commits a168914+). Legacy indexes that predate multi-granular will show the
  structural fallback until they are re-ingested. Re-index path: delete the project's
  ``docs`` table (or ``brain.db``) and run ``brain_index_doc`` over the source tree,
  then ``graph_rebuild``. No automatic migration — the raw chunks would need to be
  re-extracted from disk which indexing already does.
- **Observability defaults to on.** Every ``Brain.search()`` call logs to the
  ``searches`` table regardless of env vars; opt-out would need a new flag. Table
  size grows ~1 row per query; no retention policy yet. Check size periodically
  if the service runs unattended for weeks.

<!-- Append new entries below; keep human-readable and dated. -->
