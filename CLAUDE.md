# PRISM Project

PRISM is a software engineering methodology and Claude Code plugin with an MCP service for AI-assisted development.

## Project Knowledge

Use Brain (MCP) for all project knowledge — do not create static architecture docs.

- `brain_search` — find code, docs, patterns across the project
- `memory_recall` — recall conventions, decisions, and expertise
- `brain_call_chain` — trace call flow and blast radius from the graph

## Key Conventions

- **Never commit to**: main, master, staging, develop
- **File writes**: Max 30 lines per operation, chunk larger writes
- **Hooks**: Advisory only (exit 0), never block tool execution
- **Citations**: Read before you reference — never cite unread sources
- **Destructive ops**: Never inline PowerShell, always validate paths, never -ErrorAction SilentlyContinue

## Structure

```
.prism/
  plugins/prism-devtools/                  # Claude Code plugin (skills, commands, hooks, agents)
  services/prism-service/                  # MCP server + React SPA (pip package: prism-service)
    pyproject.toml                         # v5.3.0 — installable via pip / pipx
    prism_service/main.py                  # FastAPI + uvicorn entrypoint
    prism_service/cli/prism_cli.py         # `prism` CLI (start/stop/status/logs/update/version)
    prism_service/api/                     # JSON /api/* endpoints backing the SPA
    prism_service/routes/                  # SSE + graph viewer (non-API routes)
    prism_service/web/                     # Vite + React 19 + Tailwind v4 + @nous-research/ui
    prism_service/web_dist/                # SPA build output (gitignored, shipped as package_data)
  docs/stories/                            # Story files
  .mcp.json                                # MCP config -> localhost:7777
```

## Service ports

- **MCP** on `http://localhost:7777/mcp/?project=prism` — agent-facing tool surface (default profile `interactive`; use `tool_profile=all` for admin sessions).
- **Web UI** on `http://localhost:7778/` — React SPA. Same FastAPI process also serves `/api/*` (JSON), `/sse/sessions` (events), and `/graph/viewer/{project}` (Sigma WebGL).

Start everything (docker path — still supported for server / CI deploys):
```bash
cd services/prism-service && docker compose up -d
```

Or run natively via the v5.3.0 pip distribution:
```bash
pipx install prism-service        # isolated; recommended for end-users
prism start                       # foreground, http://localhost:7778/
prism start --daemon              # detach + pidfile under the data dir
prism status / prism logs --follow / prism stop / prism update
```

Iterate on the UI locally (HMR, hits the dockerized or pip-installed API):
```bash
cd services/prism-service/prism_service/web && npm install && npm run dev
# then open http://localhost:5173
```
