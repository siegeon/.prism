---
description: Search project knowledge through PRISM MCP Brain tools
---

# /brain Command

Use the PRISM MCP Brain tools for project knowledge:

- `brain_search` for hybrid BM25/vector/graph retrieval
- `brain_find_symbol`, `brain_find_references`, and `brain_call_chain` for code navigation
- `brain_index_doc`, `prism_refresh`, and `prism_bulk_refresh` for indexing
- `prism_status` and `prism_sync` for health and repair

Do not call plugin-local Python engines. Brain storage and indexing are owned
by `services/prism-service/`.
