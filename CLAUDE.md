# PRISM Project

PRISM is a software engineering methodology and Claude Code plugin with an MCP service for AI-assisted development.

## Project Knowledge

Use Prism (MCP) for all project knowledge — do not create static architecture docs.

- `brain_search` — find code, docs, patterns across the project (reach for it before grep)
- `memory_recall` — recall conventions, decisions, and expertise
- `brain_call_chain` — trace call flow and blast radius from the graph
- `memory_store` — write decisions back the moment they're made; PRISM is the memory layer, chat is not

## Working tasks

All in-progress work is a PRISM task driven through the conductor — never bare `/api` pokes.

- Implement a task through the conductor loop: `job = conductor_work()` → do exactly `job["instructions"]`, produce `job["expected_proof"]` → `conductor_work(id=..., outcome=..., proof=...)`. The server owns the step sequence; never hand-drive or hand-clear SDLC steps or gates.
- Create work with `task_create` (title = human-friendly WHAT, ~4-9 words; mechanics in description; define the `oracle` + `likely_misfire` up front). Watchable tasks are root tasks (`parent_id=""`).
- A gate is decided by a DISTINCT actor — the producing session cannot clear its own gate; a red test is always your fault, never "pre-existing".
- The distinct actor need not be human, but machine adjudication is OPT-IN (owner decisions 2026-07-15/16): in environments that set `PRISM_GATE_ADJUDICATOR_INTERVAL=<seconds>`, the conductor's `conductor-adjudicator` seat decides a green_gate on a FRESH PASSING EvidenceReceipt from its own trusted runner, and a red_gate for `proof_type=demo` tickets via the demo rubric (no test suite by design; proof burden stays at green_gate). Both seats must work (owner 2026-07-16): a human can always click, AND an automated user must be able to complete the same gates. Non-opted-in environments keep human clicks as the norm. The human always keeps: visibility of every machine decision, reject/override, manual-evidence oracles, and failed gates.
- The fundamental workflow: the MAIN chat thread spawns an async subagent to work a ticket through PRISM end-to-end (conductor loop, gates included); the MAIN thread then evaluates what it took — friction, visibility, cost — and refines the process. Anything that structurally prevents a subagent from completing a ticket is a product defect.

## Self-learning

When I correct you, or you catch yourself making a mistake: before continuing, add the lesson as a one-line rule under `## Lessons`, so it never happens again.

## Lessons

- Never clean up a planted/temporary line with `git checkout -- <file>` while the file holds uncommitted real work — it reverts EVERYTHING; remove the planted line with a targeted edit instead.
- On a conductor_work drive, the story/plan rubrics read `task.plan_doc` — writing the story only as step `proof=` (→ completion_proof) auto-fails story_gate with "story_md is empty"; always `task_update(plan_doc=...)` alongside the draft_story/verify_plan report.
- When writing doctrine/memory from an owner conversation, record the owner's ACTUAL intent, not my hardened absolutization of it — "human in the loop" means visibility + override, never "every gate is a human click"; an over-strict note I write becomes a wall every future session (and every supervisor) enforces against the owner's real goal.
- Evidence for PRISM work (screenshots, audit reports) goes INTO PRISM — the task evidence store + /tasks/:id/proof — never claude.ai artifacts or any external host; the owner reviews evidence where the gate is.
- DONE means SHIPPED — merged and validated on main (released), not drive-complete: a green_gate pass is "verified"; never report or display a task as done while its commits sit on an unpushed branch (owner 2026-07-16).

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
    pyproject.toml                         # installable via pip / pipx (version = __version__.py PRISM_VERSION)
    prism_service/main.py                  # FastAPI + uvicorn entrypoint
    prism_service/cli/prism_cli.py         # `prism` CLI (start/stop/status/logs/update/version)
    prism_service/api/                     # JSON /api/* endpoints backing the SPA
    prism_service/routes/                  # SSE + graph viewer (non-API routes)
    prism_service/web/                     # Vite + React 19 + Tailwind v4 + @nous-research/ui
    prism_service/web_dist/                # SPA build output (gitignored, shipped as package_data)
  docs/stories/                            # Story files
  .mcp.json                                # repo-default MCP config -> 7777 (release); ~/.claude.json overrides to 8887 (dev)
```

## Service ports

Two instances live on this machine — never mix them:

- **DEV (what sessions in this repo use)** — Windows source-run: MCP `http://127.0.0.1:8887/mcp/?project=prism`, Web UI `http://127.0.0.1:8888/`. Claude Code's prism MCP for `E:\.prism` is overridden to 8887 in `~/.claude.json`; if it's unreachable, ask the owner to start dev (`prism-dev` skill) and `/mcp` reconnect — never build an HTTP shim around it. Use `127.0.0.1`, not `localhost` (IPv6-first resolution stalls ~200ms/request). Every code change ends with this daemon bounced and `/api/version` reporting the new build.
- **RELEASE (leave alone)** — WSL pipx: MCP `http://localhost:7777/mcp/?project=prism`, Web UI `http://localhost:7778/`. The same FastAPI process serves `/api/*` (JSON), `/sse/sessions` (events), and `/graph/viewer/{project}` (Sigma WebGL) on both instances. Default tool profile is `interactive`; use `tool_profile=all` for admin sessions.

**Dev on this machine**: use the `prism-dev` skill — editable install from `E:\.prism\.venvs\dev`, source-run on 8887/8888, Edge `--app` window. Never docker/pipx/Tauri for local dev; any path >30s build/install is wrong. Patch-bump `PRISM_VERSION` on every user-visible change and bounce the daemon.

End-user / server paths (not for dev):
```bash
pipx install prism-service        # isolated; recommended for end-users
prism start --daemon              # detach + pidfile under the data dir
prism status / prism logs --follow / prism stop / prism update
cd services/prism-service && docker compose up -d   # server / CI deploys
```

Iterate on the UI with HMR (hits the running API):
```bash
cd services/prism-service/prism_service/web && npm install && npm run dev
# then open http://localhost:5173
```
