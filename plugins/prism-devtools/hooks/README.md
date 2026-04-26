# PRISM Plugin Hooks

This directory intentionally contains no executable hook runtime.

PRISM is MCP-first. Active client hooks are served by the MCP `prism_install`
tool from `services/prism-service/app/assets/` and written into the user's
`.claude/` directory.

Keeping hook implementation in the service prevents plugin-side drift. The
local `hooks.json` is a no-op so an old plugin install cannot register a
second lifecycle pipeline.
