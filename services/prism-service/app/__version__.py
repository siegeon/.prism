"""Single source of truth for PRISM's version.

Bump on user-visible changes — schema migrations, new tools, hook script
updates, install-manifest changes. Served alongside the install manifest
so users can tell which version is live and which one installed their hook.
"""

PRISM_VERSION = "4.6.0"

# Changelog-ish notes (free-form; keep short)
PRISM_VERSION_NOTES = (
    "autonomous learning loop (PostToolUse edit-learn + Stop idle-rebuild "
    "hooks), cwd-robust hook commands via ${CLAUDE_PROJECT_DIR}, default "
    "ports moved to MCP=7777 / UI=7778 (breaking — update .mcp.json), "
    "graphifyy pinned to 0.4.29 (>=0.4.29,<0.5)"
)
