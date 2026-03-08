# Semver Guide for prism-devtools

## Why Every Change Needs a Version Bump

Claude Code uses the version string from `plugin.json` to determine the cache path:
`~/.claude/plugins/cache/prism/prism-devtools/{VERSION}/`

**If you change code but don't bump the version, users will never see the update.**
This is due to known caching bugs:
- [#17361](https://github.com/anthropics/claude-code/issues/17361): Cache never refreshes
- [#15642](https://github.com/anthropics/claude-code/issues/15642): CLAUDE_PLUGIN_ROOT points to stale version
- [#14061](https://github.com/anthropics/claude-code/issues/14061): `/plugin update` doesn't invalidate cache

## Version Format

```
MAJOR.MINOR.PATCH
  │     │     └── Bug fixes, prompt tweaks, doc updates
  │     └──────── New features (backward-compatible)
  └────────────── Breaking changes (incompatible)
```

## What Counts as Each Bump Type

### PATCH (3.5.0 → 3.5.1)
- Fix a bug in a script
- Update prompt text or persona wording
- Fix a typo in SKILL.md
- Update reference documentation
- Fix hook behavior (non-breaking)

### MINOR (3.5.0 → 3.6.0)
- Add a new skill
- Add a new slash command
- Add a new hook
- Add a new agent capability
- Add new collectors to prism-bug

### MAJOR (3.5.0 → 4.0.0)
- Remove or rename a skill/command
- Change hook behavior in a breaking way
- Change the plugin directory structure
- Remove a feature users depend on

## Version Source of Truth

| Location | Purpose | Must Match |
|----------|---------|------------|
| `plugin.json` | **Primary** — Claude Code reads this | Always correct |
| `CHANGELOG.md` | Documentation — users read this | Should match |
| Git tags | Release markers — enables pinning | Should match |

## Distribution

This plugin is distributed via the `siegeon/.prism` GitHub repo. Users install it as a
Claude Code marketplace plugin. When `autoUpdate` is enabled, Claude Code checks for
new versions at startup and updates the cache if the version string changed.

## Pre-release Versions

Claude Code supports pre-release versions like `3.6.0-beta.1`. Use these for testing
changes before a stable release. Pre-release versions sort lower than their release
counterpart, so `3.6.0-beta.1 < 3.6.0`.
