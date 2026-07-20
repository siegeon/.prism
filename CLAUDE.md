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
- On a conductor_work drive, the story/plan rubrics read `task.plan_doc`, and the draft_story/verify_plan `proof=` OVERWRITES plan_doc — so pass the FULL story/plan markdown as `proof=`; a `task_update(plan_doc=...)` made beforehand is clobbered by the very next report, and a one-shot rubric autoclear miss parks story_gate/plan_gate pending until the adjudicator's rubric re-sweep (v7.0.49) lifts it — keep the report compliant at the moment it lands anyway.
- When writing doctrine/memory from an owner conversation, record the owner's ACTUAL intent, not my hardened absolutization of it — "human in the loop" means visibility + override, never "every gate is a human click"; an over-strict note I write becomes a wall every future session (and every supervisor) enforces against the owner's real goal.
- Evidence for PRISM work (screenshots, audit reports) goes INTO PRISM — the task evidence store + /tasks/:id/proof — never claude.ai artifacts or any external host; the owner reviews evidence where the gate is.
- DONE means SHIPPED — merged and validated on main (released), not drive-complete: a green_gate pass is "verified"; never report or display a task as done while its commits sit on an unpushed branch (owner 2026-07-16).
- Bouncing the dev daemon from a sandboxed background shell ties it to that shell's Windows job — it dies silently (no traceback, mid-request logs) when the harness recycles the shell; launch with the sandbox disabled (or F5) so the daemon survives.
- A lane must commit its failing tests as a TESTS-ONLY `[task:<id>]` commit at the red step, BEFORE any implementation commit — the red machine seat anchors to that commit; bundling tests+impl makes red undemonstrable and strands red_gate with a human.
- Slice scoping must check control_plane.POLICY_FILES up front — a task whose allowed_files includes a gate-policy file will fail its own gates on the candidate-controls-judge tooth; put consumers of policy modules in a separate non-policy file, and never let a producer tag its own task policy-change mid-drive.
- Long curl -d JSON bodies with unicode (em-dashes) silently fail through the shell — write the body to a file and use --data-binary @file.
- Committing a test-proof lane's failing tests to the WORKING BRANCH instead of the per-task scratch worktree used to strand red_gate forever (the anchor stamped from the lagging worktree HEAD landed pre-tests → "no tests ran"); FIXED v7.1.18 (mx-6c73be): the red anchor now self-heals to the committed tests-only `[task:<id>]` commit and mints red on-demand at approval — but the durable habit is still to let the red machine seat anchor cleanly, and NEVER reach for a bare `/api/conductor/rewind` poke (no MCP tool + classifier-blocked) to unstick a strand; fix the anchor logic, don't hand-repair.
- A machine-adjudicated rubric/red gate that parks PENDING writes NO gate_reason to the task — a driving subagent can't self-diagnose and pings a human at every gate; the real reason is computed (story needs `AC-<n>` not `AC1`; plan_gate reads the plan_diagram FIELD not a ```mermaid fence in plan_doc; red wants rc==1 and gets rc=4 "no tests ran" when task.verify isn't workspace-root-relative). Read the rubric/receipt to unblock; set `task.verify` to a workspace-root-relative path (`services/prism-service/tests/...`, not `cd services/prism-service && pytest tests/...`) at creation. Fix tracked in task 8f48f9bb.

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
