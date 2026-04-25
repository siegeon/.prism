# prism-devtools

Claude Code plugin that bridges editor lifecycle events to the
PRISM MCP server (lives at `services/prism-service/` in the repo
root). Orchestration —
workflow state, task management, gate decisions, expertise — lives in
the server, exposed via MCP tools (`brain_*`, `memory_*`, `task_*`,
`workflow_*`). The plugin's job is keeping the server fed and surfacing
a few thin slash-command shortcuts.

## What's in here

### `hooks/` — Brain/Janitor feed
Bridges Claude Code lifecycle events into MCP calls so the server
stays current without manual re-indexing.

| Hook | When | What it does |
|------|------|--------------|
| `session-start.py` | SessionStart | Bootstrap Brain, run incremental reindex |
| `capture-commit-context.py` | PostToolUse:Bash (git commit) | Stream commit context into Brain |
| `capture-file-context.py` | PostToolUse:Edit/Write | Trigger debounced incremental reindex |
| `feedback_signal_hook.py` | PostToolUse:brain_search/Read/Edit/Write | Implicit retrieval thumbs-up via `brain_search_feedback` |
| `save-large-responses.py` | PostToolUse:Read/Grep/Glob | Spill >50-line responses to disk to reduce context bloat |
| `pre-read-token-guard.py` | PreToolUse:Read | Warn on redundant re-reads |
| `pre-write-convention-guard.py` | PreToolUse:Edit/Write | Check writes against conventions in Memory |
| `log-terminal-output.py` | PostToolUse:Bash | Persist terminal output for later grep |

### `commands/` — slash-command proxies
- `/brain` — search Brain (BM25 + vector + graph hybrid)
- `/conductor` — prompt-optimization tools (scoring, variant generation)

### `skills/` — general-purpose skills
`brain`, `conductor`, `jira`, `file-first`, `version-bump`,
`hooks-manager`, `init-context`, `document-project`,
`investigate-root-cause`, `remember`, `agent-builder`, `skill-builder`,
`validate`, `validate-issue`, `shared`. Each has its own `SKILL.md`
with usage and references.

### `agents/` — general validators
`link-checker`, `lint-checker`, `portability-checker`,
`terminology-checker`, `test-runner`. Pure validators with no
workflow coupling.

### `scripts/` — repo-level tooling
Validation and portability checks used by the pre-commit hook.

## What it does not do

- No workflow state machine. The MCP server owns `workflow_advance`,
  `workflow_state`, and the phase transitions.
- No story/gate/persona orchestration. If you want phase-driven
  development, drive it directly via the MCP `workflow_*` and `task_*`
  tools or build your own thin layer on top.
- No auto-commits, no auto-advance on Stop, no story-context gating
  on Bash. Those duplicated logic that belongs in the server.

## Install

The plugin is installed via Claude Code's plugin marketplace pointing
at this repo. The MCP server (`services/prism-service/`) must be
running locally (`docker compose up -d`) for the hooks and skills to
be useful.

See [`hooks/README.md`](hooks/README.md) for hook-level details and
[`skills/README.md`](skills/README.md) for the skill index.
