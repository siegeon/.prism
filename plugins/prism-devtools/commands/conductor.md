---
description: Inspect prompt optimization data through PRISM MCP
---

# /conductor Command

Conductor data is owned by the PRISM MCP service.

Use MCP tools such as `record_outcome`, `record_skill_usage`, and
`record_subagent_outcome`, plus the PRISM web UI, to inspect or update prompt
and skill scoring state.

Do not call plugin-local Python engines. Scores and variants are stored in the
service's project-scoped SQLite data directory.
