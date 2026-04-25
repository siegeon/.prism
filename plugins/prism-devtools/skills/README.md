# prism-devtools skills

General-purpose skills shipped with the plugin. Workflow orchestration
(stories, gates, personas) lives in the PRISM MCP server
(`services/prism-service/` in the repo root), not here — invoke its
`task_*` and `workflow_*` MCP tools directly.

## Project knowledge

| Skill | Purpose |
|-------|---------|
| [brain](./brain/SKILL.md) | Hybrid BM25 + vector + graph search via Brain MCP |
| [conductor](./conductor/SKILL.md) | Prompt-optimization tools (scoring, variant generation) |
| [remember](./remember/SKILL.md) | Persist a conviction or pattern via `memory_store` |
| [shared](./shared/SKILL.md) | Cross-skill reference docs |

## Codebase navigation

| Skill | Purpose |
|-------|---------|
| [file-first](./file-first/SKILL.md) | Read source files directly; no RAG, no preloaded summaries |
| [investigate-root-cause](./investigate-root-cause/SKILL.md) | Trace bugs via code analysis, git history, error tracing |
| [validate-issue](./validate-issue/SKILL.md) | Reproduce and document a reported issue |
| [document-project](./document-project/SKILL.md) | Generate reference documentation for a codebase |

## Tooling

| Skill | Purpose |
|-------|---------|
| [hooks-manager](./hooks-manager/SKILL.md) | Create, configure, debug Claude Code hooks |
| [skill-builder](./skill-builder/SKILL.md) | Build, optimize, validate skills |
| [agent-builder](./agent-builder/SKILL.md) | Build custom sub-agents |
| [version-bump](./version-bump/SKILL.md) | Bump plugin version, update CHANGELOG, tag |
| [validate](./validate/SKILL.md) | Run plugin docs/links/portability checks |
| [init-context](./init-context/SKILL.md) | Bootstrap a project's `.context/` folder |

## Integrations

| Skill | Purpose |
|-------|---------|
| [jira](./jira/SKILL.md) | Jira issue search, fetch, planning |
