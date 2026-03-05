---
name: brain
description: Search project knowledge - code, docs, expertise, patterns
---

# Brain — Project Knowledge Search

Search across code, documentation, mulch expertise, and metrics
using semantic + keyword + structural search.

## Commands

### /brain search <query>
Search all indexed knowledge. Returns ranked results from:
- Code (AST-chunked, semantic matching)
- Documentation (architecture, PRDs, READMEs)
- Mulch expertise (conventions, patterns, decisions)
- Metrics (agent performance, test results)

### /brain status
Show index health: chunk count, entity count, degradation mode,
last indexed timestamp.

### /brain init
Full index of all project sources. Runs automatically on first
prism-loop but can be triggered manually.

### /brain ingest
Re-index everything. Use after major changes or to pick up
new source types.

## Architecture

See [Brain Architecture](./reference/brain-architecture.md) for:
- Three-index search design (BM25 + Vector + GraphRAG)
- Persistence model (brain.db vs scores.db)
- Ingestion pipeline and source types
- RRF fusion algorithm
