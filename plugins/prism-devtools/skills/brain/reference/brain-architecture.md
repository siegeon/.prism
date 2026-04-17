# Brain Architecture

## Overview

Brain provides three-index hybrid search over all project knowledge:
code, docs, mulch expertise, and metrics. It runs entirely locally
with no external APIs or servers.

## Tech Stack

| Component | Role | Size | Startup |
|-----------|------|------|---------|
| SQLite FTS5 | BM25 keyword search | 0 (stdlib) | instant |
| model2vec | Embedding generation | ~30MB model | <100ms |
| sqlite-vec | Vector similarity search | <1MB | instant |
| SQLite tables | GraphRAG relationships | 0 (stdlib) | instant |
| tree-sitter-languages | AST parsing (11 langs) | ~20MB | instant |

**Total footprint:** ~50MB. No PyTorch. No external APIs. No servers.

## Three Indexes

### Index 1: BM25 (keyword precision)
FTS5-backed keyword search. Finds exact matches — symbol names,
error codes, specific terms.

### Index 2: Vector (semantic understanding)
model2vec embeddings (potion-base-32M). Finds conceptual matches
even when words differ. Query: "how does login work" finds auth
code, JWT handling, session management.

### Index 3: GraphRAG (structural relationships)
SQLite tables for entity/relationship graph built from tree-sitter
AST analysis.

```sql
CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    name TEXT,    -- "AuthService", "authenticate"
    kind TEXT,    -- "class", "function", "module", "file"
    file TEXT,
    line INTEGER
);

CREATE TABLE relationships (
    source_id INTEGER REFERENCES entities(id),
    target_id INTEGER REFERENCES entities(id),
    relation TEXT  -- "calls", "imports", "extends", "contains"
);
```

Answers structural queries: "what calls authenticate",
"dependencies of payment module".

## Reciprocal Rank Fusion (RRF)

All three indexes are queried in parallel. Results are merged using
RRF (k=60), which combines rankings without requiring score
normalization. Final results are reranked by combined signal.

## Persistence Architecture

Storage is split by volatility:

```
.prism/brain/
├── brain.db       # REBUILDABLE: FTS5 + vec indexes (gitignored)
├── graph.db       # REBUILDABLE: entity/relationship graph (gitignored)
├── scores.db      # PERSISTENT: learned prompt scores (git-committed)
├── outcomes.jsonl # PERSISTENT: raw execution history (git-committed)
├── config.yaml    # PERSISTENT: brain settings
└── .gitignore     # Ignores brain.db and graph.db only
```

`brain.db` and `graph.db` can always be rebuilt from source via
`brain ingest`. `scores.db` and `outcomes.jsonl` contain learned
knowledge and must be committed to survive rebuilds.

## Ingestion Sources

Brain indexes all of:
- **code** — Project source files, AST-chunked with tree-sitter
- **docs** — Markdown documentation (architecture, PRDs, READMEs)
- **mulch** — `.mulch/expertise/*.jsonl` (conventions, patterns, decisions)
- **overstory** — `.overstory/` metrics and agent history
- **git** — Recent commit history

## Brain API

```python
class Brain:
    def search(self, query, domain=None, limit=5) -> list
    def system_context(self, story_file, persona, limit=8) -> str
    def graph_query(self, entity, relation, limit=10) -> list
    def ingest(self, sources: list[str])
    def incremental_reindex(self)
    def record_outcome(self, prompt_id, persona, step_id, metrics)
    def avg_tokens(self, step_id) -> int
    def outcome_count(self, persona, step_id) -> int
    def top_outcomes(self, persona, step_id, limit=5) -> list
```

## Offline / Degraded Mode

If dependencies are missing, Brain degrades gracefully:
- No model2vec → vector search disabled, BM25+graph only
- No sqlite-vec → same as above
- No tree-sitter → graph search disabled, BM25+vector only
- brain.db missing → full text search disabled
- All indexes missing → Brain returns empty, Conductor falls back
  to static ROLE_CARDS (zero regression)

## Incremental Reindexing

After each step, the stop hook calls `incremental_reindex()`.
This re-indexes only files changed since the last index run
(via `git diff --name-only HEAD~1`), keeping the index fresh
without a full rebuild on every step.
