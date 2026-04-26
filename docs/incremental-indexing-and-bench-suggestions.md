# Incremental indexing + native bench surface — suggestions

A scratchpad of where to take PRISM's retrieval stack next, written after
watching ["I Stopped Using Grep and My Agent Got 10x Faster"](https://www.youtube.com/watch?v=gPeWb4_DMok)
(zilliztech/claude-context) and comparing to what we already ship. Three
threads: a borrowable indexing technique, a free-embedder lineup so we
never reach for paid APIs, and the external benchmarks the field uses so
we can wire them into `benchmarks/` over time.

## Hard constraint

**No paid LLM embeddings.** Everything below has to run locally on CPU
via `sentence-transformers` (or equivalent) with no API key. Claude
Context's default is OpenAI `text-embedding-3-small`; we are not
adopting that. `all-MiniLM-L6-v2` is the floor we already meet, and the
2026-04-19 Run log shows it gives us +0.110 R@5 over potion at zero cost.

## What's already shipped (so we don't re-propose it)

- AST-based chunking (multi-granular: file + entity + sliding window).
- Hybrid BM25 + vector + graph search with RRF fusion.
- Anthropic-style contextual chunk prefix (default on, justified by the
  2026-04-21 SWE-bench A/B that cratered R@10 0.900 → 0.700 with prefix off).
- Per-doc `content_hash` and a `prism_status(file_hashes=...)` drift API.
- Threshold gate (`benchmarks/assert_thresholds.py`) for any retrieval-side
  change: R@5 ≥ 0.96, +2 pool sessions, ≤1.6× latency.

## Suggestion 1 — Merkle DAG hashing for incremental indexing

### Problem

`prism_status(file_hashes=...)` is O(N): every file in the repo gets
hashed and compared on every sync. For a `git pull` that touched 5 files
in a 10K-file repo, we still hash all 10K to find the 5. Cold-start full
indexing is ingest-dominated (LongMemEval smoke 31 min, SWE-bench limit-10
~4.3 hrs) and that part is unavoidable; the *re-sync* path shouldn't be.

### Design

Maintain a tree of hashes alongside the existing `docs` table:

- Leaf hash = the file's `content_hash` (already computed at ingest, no
  new work).
- Internal-node hash = SHA256 of its children's hashes, sorted by name.
- Persist as `tree_hashes(project_id, dir_path, hash, mtime)` keyed by
  `(project_id, dir_path)`.

On `prism_sync` for a full Merkle implementation:

1. Compute the on-disk root hash from a single directory walk.
2. If equal to stored root → return `{changed: 0}`.
3. If different → recurse top-down, hashing files only inside subtrees
   whose directory hash differs. **O(log N + Δ).**
4. Re-ingest the changed leaves through the existing chunk/embed/graph
   pipeline; bubble the updated hashes back up to the root.

Important correction from measurement: step 2 is not actually O(1) if
PRISM computes the root from disk on every run. The scan still has to
walk/stat the tree. A true O(1) no-op needs a trusted external invalidator
such as a filesystem watcher, a git tree object, or already-known changed
paths.

### Bolt-on points

- `services/prism-service/app/engines/brain_engine.py` — extend the ingest
  path to write parent-dir hashes alongside per-doc `content_hash`.
- `services/prism-service/app/services/brain_service.py` — `prism_sync`
  and `prism_status` short-circuit on root match.
- New table + migration: `tree_hashes`. Existing schema is untouched.

### Expected impact

| Workflow | Before | Metadata-cache first cut | Full Merkle later |
|---|---|---|
| Idempotent sync (no on-disk changes) | O(N) — read/hash every file | O(N) stats, no reads | O(1) only with trusted external invalidation; otherwise O(N) stats |
| Small-delta sync (5 files of 10K) | O(N) reads/hashes | O(N) stats + 5 reads/hashes | O(log N + 5) if directory invalidation is trusted |
| First-time index | O(N) ingest | O(N) ingest (unchanged) | O(N) ingest (unchanged) |
| Move/rename across subtrees | full re-hash of touched subtrees | stat detects path churn; blob reuse is optional follow-up | same — no claim of further speedup |

Zero impact on retrieval quality (R@5/R@10 unchanged) — this is a
sync-latency win only. The bench harness shouldn't see any movement on
LongMemEval or SWE-bench because both rebuild from scratch each run; the
right way to measure the win is a `prism_sync` micro-bench (see § Bench
surface to grow → Index hygiene below).

### Experiment result — 2026-04-25

Added `benchmarks/sync/run.py` to measure the current SessionStart
hook-shaped scan against a lower-complexity metadata cache shape:

- Current path: read and SHA256 every eligible tracked source file.
- Cache path: stat every eligible tracked source file; only read/hash
  files whose `(mtime_ns, size)` differs from a persisted cache.

Results:

| Corpus | Eligible files | Eligible bytes | Current full hash | Metadata-cache no-op | Metadata-cache one-file delta |
|---|---:|---:|---:|---:|---:|
| PRISM repo | 341 | 2.9 MB | 26.9 ms | 7.1 ms | 6.9 ms |
| Django bench checkout | 2,130 | 14.4 MB | 143.9 ms | 41.1 ms | 42.6 ms |

Read: the optimization is real (roughly 3-4x on the scan step), but the
absolute cost of the current hook is not yet painful on repos this size.
The full Merkle DAG is probably overbuilt as a first implementation.
Also, the `O(1)` no-op claim only holds if something external tells us
the root is unchanged (for example a watcher, trusted git tree object, or
persisted directory metadata with correct invalidation). If PRISM computes
the root from disk on every SessionStart, it still has to walk/stat the
tree.

Recommendation after measurement: ship a persistent file metadata/hash
cache before a Merkle DAG. It gives most of the practical win, has no
retrieval-quality risk, and can be validated with the new sync benchmark.
Defer Merkle DAG until no-op sync is regularly above ~500 ms on real
operator projects or we need multi-root directory invalidation.

### Edge cases the implementer should handle

- **`git mv` across subtrees** — both source and destination dir hashes
  change; correct fallback is to re-hash both subtrees but preserve the
  doc's `content_hash` (same blob, new path). No re-embedding needed.
- **Symlinks** — follow once, detect cycles. Same policy as the existing
  walker.
- **`.gitignore` and the bench scratch dirs** — exclude `repos/`,
  `data-bench/`, `.prism/`, `node_modules/` from the tree hash; otherwise
  the root will churn on every benchmark run.
- **Concurrent writes during sync** — take a read lock on `tree_hashes`
  for the duration of one sync to avoid torn reads. Existing SQLite WAL
  is sufficient.

## Suggestion 2 — Native (free) embedder lineup

### What's already been A/B-tested in the Run log

| Model | Params | LongMemEval smoke R@5 | Verdict |
|---|---|---|---|
| `all-MiniLM-L6-v2` | 22M | 0.800 | ✅ default |
| `BAAI/bge-small-en-v1.5` | 33M | 0.820 | ↔️ no edge |
| `jinaai/jina-code-embeddings` | 137M | 0.060 | ❌ wrong corpus |
| `nomic-ai/nomic-embed-code` | — | 0.000 | ❌ wrong corpus |
| `potion-base-32M` (model2vec) | 32M | — / 0.524 full | anchor only |

Read: conversational corpora (LongMemEval) reward generic English
embedders; code-trained embedders regress badly on prose. SWE-bench
should be the inverse — but the prefix A/B already isolated the prefix
header as the dominant signal there, so swapping embedders on code is a
smaller lever than people expect.

### Untested-but-worth-a-Run-log-row candidates

All run via `sentence-transformers`, all CPU-runnable, all free:

| Model | Params | MTEB-en (approx) | Why |
|---|---|---|---|
| `intfloat/e5-small-v2` | 33M | 59.9 | MiniLM peer, slightly retrieval-tuned |
| `intfloat/e5-base-v2` | 110M | 61.5 | Cheap step-up if e5-small moves |
| `BAAI/bge-base-en-v1.5` | 110M | 63.5 | Step-up from already-tested bge-small |
| `Snowflake/snowflake-arctic-embed-m` | 110M | 64.5 | Top open in mid-class |
| `mixedbread-ai/mxbai-embed-large-v1` | 335M | 64.7 | Heaviest worth CPU testing |
| `nomic-ai/nomic-embed-text-v1.5` | 137M | 62.4 | Different corpus from `nomic-code` |

The MTEB column is a sanity-check filter, not a ranking — every candidate
above also has to clear the local promotion gate on PRISM's actual smoke
tests before any default flips. The standard ranking source is the
[MTEB leaderboard](https://huggingface.co/spaces/mteb/leaderboard);
filter to "Open" + "≤500M params" + "English" to find CPU-friendly winners.

### What "good enough native" means in practice

The 2026-04-21 SWE-bench prefix A/B shows the dominant retrieval signal
in PRISM is **structural** (chunk-level metadata, multi-granular AST,
graph relationships) not **embedding-quality**. Replacing MiniLM with a
larger free model would maybe move R@5 by +0.02–0.05 on smoke; replacing
it with a paid OpenAI/Voyage embedder would maybe add another +0.02 on
top — and that ceiling has to repay continuous re-indexing costs.

Recommendation: stay on MiniLM unless one of the candidates above clears
the promotion gate. Don't add a paid embedder branch to the codebase.

## Suggestion 3 — External benchmarks the field uses

Currently we track three benches: LongMemEval-S, SWE-bench Lite, and our
internal contextpack gate. The field has more, and most are free to run.

### Code retrieval benchmarks

| Bench | Why we'd add it |
|---|---|
| [CodeSearchNet](https://github.com/github/CodeSearchNet) (HF dataset, 2M code-comment pairs across 6 languages) | Classic code-retrieval benchmark; reports MRR. Fast smoke. |
| [CoIR-bench](https://github.com/CoIR-team/coir) (Code Information Retrieval, 2024, 10 tasks) | Multi-task: text-to-code, code-to-code, code-to-text. Single suite, much broader than CodeSearchNet. |
| [CodeRAG-Bench](https://github.com/code-rag-bench/code-rag-bench) (2024) | Specifically the RAG-for-code workflow we're optimizing for. |
| [RepoBench](https://github.com/Leolty/repobench) | Repo-level code completion with retrieval; closer to PRISM's actual use case than per-file localization. |

### Long-context / memory benchmarks

| Bench | Why we'd add it |
|---|---|
| [LoCoMo](https://github.com/snap-research/locomo) | Long conversational memory, 600 sessions, ~9K tokens each. Direct companion to LongMemEval. |
| [NoLiMa](https://github.com/adobe-research/NoLiMa) (2024) | Needle-in-a-haystack but the needle is *semantically* related, not lexically — actually tests retrieval quality vs string matching. |
| [InfiniteBench](https://github.com/OpenBMB/InfiniteBench) | 100K+ token contexts; tests scaling not just quality. |
| [RULER](https://github.com/NVIDIA/RULER) | NVIDIA's long-context eval; controls for confounders better than NIAH. |

### Embedder leaderboards (use to pick candidates without running benches)

| Resource | What it gives you |
|---|---|
| [MTEB leaderboard](https://huggingface.co/spaces/mteb/leaderboard) | The de-facto standard; 56 datasets across retrieval / STS / classification / clustering. Filter to open-source + small for CPU-friendly winners. |
| [BEIR](https://github.com/beir-cellar/beir) | 18 retrieval datasets; standalone-runnable subset of MTEB. |

### RAG quality (downstream — partial paid-LLM dependency)

| Framework | Cost note |
|---|---|
| [RAGAS](https://docs.ragas.io) | **Context Precision** and **Context Recall** can run with claim-level matching against a reference answer — no LLM judge needed for these two. **Faithfulness** and **Answer Relevance** require an LLM judge (paid; defer until retrieval-quality benches saturate). |
| [BEIR's NDCG@10](https://github.com/beir-cellar/beir) | Standard rank-aware metric. Complement to our hit-rate-shaped R@K — answers "even when we hit, was the gold ranked first?" |

### Index hygiene (we don't measure these yet)

These don't have public datasets — they're operational metrics PRISM
should track on its own corpus. Fits naturally as a `benchmarks/sync/`
micro-bench that any future work on Suggestion 1 can validate against.

| Metric | What it captures |
|---|---|
| `prism_sync` p50 / p95 wall time on no-op | Should drop to single-digit ms after Suggestion 1. |
| `prism_sync` wall time on a 5-file delta in PRISM itself | Should drop to <500ms. |
| Index disk size per 1M source tokens | Sanity check — multi-granular chunking inflates this; want a baseline before any future chunking change. |
| Cold-start full-ingest time for PRISM's own repo | Currently uncaptured; would catch regressions in the chunk pipeline. |

## What people use to measure all of this in one breath

The shorthand the field reaches for, mapped to where each metric belongs
in PRISM's bench harness:

| Metric | Used by | Where in PRISM |
|---|---|---|
| **Recall@K** (hit rate) | LongMemEval, SWE-bench, CoIR, BEIR | Already shipped — `recall@5`, `R@1/5/10` |
| **NDCG@K** (rank-aware) | BEIR, MTEB retrieval tasks | Not yet — add when adopting BEIR |
| **MRR** (mean reciprocal rank) | CodeSearchNet, RepoBench | Not yet — add when adopting CodeSearchNet |
| **`pool_recall@K`** (candidate generation) | PRISM-internal (PLAT-0042) | Already shipped — diagnoses ceilings |
| **Median / p95 latency** | Any production system | Already shipped — `median_ms` |
| **Context Precision / Recall** | RAGAS | Not yet — needs RAGAS adoption |
| **Faithfulness / Answer Relevance** | RAGAS, TruLens | Defer — needs LLM judge |
| **Index size / sync latency** | DB / IR ops | Not yet — needs the `benchmarks/sync/` harness from Suggestion 1 |

## Non-goals

- **Paid embedders** — OpenAI `text-embedding-3-*`, Voyage, Cohere, Gemini.
  Marginal MTEB lift over free top-tier (e5-base, bge-base, mxbai) is
  small; the per-token bill on continuous re-indexing isn't.
- **Managed vector DBs** — Zilliz Cloud, Pinecone, Weaviate Cloud. We
  ship `vec0` (sqlite-vec) embedded in the same SQLite as BM25. Single
  file, zero ops, no deploy step. That's a feature, not a limitation.
- **LLM-as-judge benches** — RAGAS Faithfulness, RAGAS Answer Relevance,
  TruLens RAG triad. All need a paid model. Defer until the
  retrieval-quality benches above are saturated and we've run out of
  cheaper signals.
- **Replicating Claude Context's plugin shape** — they ship a VSCode
  extension and a CLI. PRISM is MCP-first; any client that speaks MCP
  already gets the index. Don't add tool-specific adapters.

## Suggested order of work for whoever picks this up

1. **Sync metadata cache** (Suggestion 1, measured first cut) — biggest
   practical UX win for the least machinery. Persist `(path, size,
   mtime_ns, sha256)` for the SessionStart hook/status path; stat every
   eligible file and hash only changed files. Validate with
   `benchmarks/sync/run.py`, not LongMemEval. Defer full Merkle DAG until
   measured no-op sync cost justifies directory-level invalidation.
2. **NDCG@K + MRR in the existing harness** — single-file change to
   `benchmarks/longmemeval/run.py` and `swebench/run.py`; gives us
   rank-aware metrics for free without adopting any new bench.
3. **CoIR-bench or CodeRAG-Bench** — pick one; they're the next bench
   to add after SWE-bench because they're code-focused and free.
4. **Embedder A/B sweep** — only after the above. Run e5-base and
   bge-base through the smoke against MiniLM. Promote one or none.

Anything below #4 (CodeSearchNet, RepoBench, LoCoMo, NoLiMa, InfiniteBench,
RULER, RAGAS) is overflow — pick whichever the next regression scare
points at.
