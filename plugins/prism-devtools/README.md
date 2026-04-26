# prism-devtools

Client-side source material for PRISM.

The PRISM service in `services/prism-service/` is the source of truth for MCP
tools, persistence, workflow state, context building, and hook installation.
This directory holds reusable client-facing material only: commands, agents,
skills, prompts, templates, docs, and validation scripts.

## MCP-First Boundary

- **Service-owned:** Brain, Memory, Tasks, Workflow, ContextBuilder,
  `prism_install`, and all active lifecycle hook implementations.
- **Plugin-owned:** source material that can be consumed by clients or by the
  MCP context builder, such as persona prompts, templates, skills, commands,
  and validator agent specs.
- **No plugin runtime:** `hooks/hooks.json` is intentionally a no-op. Active
  hooks are distributed by the MCP `prism_install` route from
  `services/prism-service/app/assets/`.

## Install

Start the MCP service, connect a project through `.mcp.json`, then call
`prism_install` from the MCP server. Re-run `prism_install` whenever the
service updates hook or client-adapter assets.

## What Remains Here

- `commands/` — thin client-facing command docs
- `agents/` — validator and role source material
- `skills/` — reusable client skill instructions
- `prompts/` — persona prompt source material
- `templates/` — document and context templates
- `hooks/` — no-op plugin hook registration
