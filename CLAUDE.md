# PRISM Project

PRISM is a software engineering methodology and Claude Code plugin with an MCP service for AI-assisted development.

## Project Knowledge

Use Prism (MCP) for all project knowledge — do not create static architecture docs.

- `brain_search` — find code, docs, patterns across the project (reach for it before grep)
- `memory_recall` — recall conventions, decisions, and expertise
- `brain_call_chain` — trace call flow and blast radius from the graph
- `memory_store` — write decisions back the moment they're made; PRISM is the memory layer, chat is not

## Writing standard for PRISM content

Everything written INTO PRISM — task titles, descriptions, oracles, stop_if lines, memories, ontology labels and comments, rule messages — follows ASD-STE100 Simplified Technical English via the vendored `asd-ste100` skill (`.claude/skills/asd-ste100/SKILL.md`, owner 2026-08-26). Strict mode for instructions (oracle, stop_if, likely_misfire, error strings, tool descriptions); STE-flavored for descriptions and memories. The application enforces the regex-checkable subset itself: `prism_service/services/ste.py` normalises every task/memory write and returns a `style` block, and the ontology rule `text-is-plain` flags what survives on the Rules tab. Keep every hedge — a rewrite that turns "may have failed" into "failed" is a different claim, not a simplification.

## Working tasks

All in-progress work is a PRISM task driven through the conductor — never bare `/api` pokes.

**THE CANONICAL DOCTRINE LIVES IN PRISM, NOT HERE**: call `prism_guide(section="conductor")` before driving a task. It owns the loop, the two human gates, evidence freshness (re-mint after ANY commit that moves the tree), why `readiness` is not the decider, why Override cannot skip the oracle, and filing follow-ups as subtasks. Add new conductor doctrine THERE (`mcp/tools.py` `_GUIDE_SECTIONS["conductor"]`) so every consumer inherits it — this file stays a pointer, not a second copy that drifts.

- Implement a task through the conductor loop: `job = conductor_work()` → do exactly `job["instructions"]`, produce `job["expected_proof"]` → `conductor_work(id=..., outcome=..., proof=...)`. The server owns the step sequence; never hand-drive or hand-clear SDLC steps or gates.
- Create work with `task_create` (title = human-friendly WHAT, ~4-9 words; mechanics in description; define the `oracle` + `likely_misfire` up front). Watchable tasks are root tasks (`parent_id=""`).
- A gate is decided by a DISTINCT actor — the producing session cannot clear its own gate; a red test is always your fault, never "pre-existing".
- The distinct actor need not be human, but machine adjudication is OPT-IN (owner decisions 2026-07-15/16): in environments that set `PRISM_GATE_ADJUDICATOR_INTERVAL=<seconds>`, the conductor's `conductor-adjudicator` seat decides a green_gate on a FRESH PASSING EvidenceReceipt from its own trusted runner, and a red_gate for `proof_type=demo` tickets via the demo rubric (no test suite by design; proof burden stays at green_gate). Both seats must work (owner 2026-07-16): a human can always click, AND an automated user must be able to complete the same gates. Non-opted-in environments keep human clicks as the norm. The human always keeps: visibility of every machine decision, reject/override, manual-evidence oracles, and failed gates.
- The fundamental workflow: the MAIN chat thread spawns an async subagent to work a ticket through PRISM end-to-end (conductor loop, gates included); the MAIN thread then evaluates what it took — friction, visibility, cost — and refines the process. Anything that structurally prevents a subagent from completing a ticket is a product defect.

## Self-learning

When I correct you, or you catch yourself making a mistake: before continuing, append the lesson to the `prism-lessons` skill (NOT to this file, which stays lean), so it never happens again.

## Lessons

90 hard-won lessons from real drives live in the **`prism-lessons` skill**
(`.claude/skills/prism-lessons/SKILL.md`) -- moved out of this file 2026-09-08
because they were 87% of it and ~16.5k tokens of every session preload.

**Load that skill before** driving a PRISM task, deciding or reporting a gate,
bouncing the daemon, or shipping to dev/main. They are indexed by topic; the
detail in each one is the point, so read the whole lesson, not the index line.

## Key Conventions

- **Never commit to**: main, master, staging, develop — EXCEPT this repo's own self-development (PRISM building PRISM). Owner standing authorization (2026-08-21, reconfirmed after a fresh agent's direct push to `dev` was flagged as a policy violation): a session/agent working ON `prism-service` itself may commit and push directly to `dev` and `main` — no feature branch, no PR, no `/ship` — following the established per-fix ritual (bump `PRISM_VERSION` with a changelog entry, run the relevant tests, commit, `git fetch . <local-branch>:dev`, `git push origin dev`, then the same for `main` with `--no-verify` since the pre-push hook is permanently broken, see the Lessons entry on this). This exception is scoped to `prism-service`'s own codebase; it is NOT a general license — driving a task through the conductor for someone else's feature still goes through the conductor's own gates, and other Bespoke Labs products keep their normal branch protection.
- **File writes**: Max 30 lines per operation, chunk larger writes
- **Hooks**: Advisory only (exit 0), never block tool execution
- **Citations**: Read before you reference — never cite unread sources
- **Destructive ops**: Never inline PowerShell, always validate paths, never -ErrorAction SilentlyContinue

## Service ports

Three instances have been observed live on this machine — never mix them up, and confirm which one a given session is actually talking to before trusting doc guidance below over a live `curl .../api/version` check:

- **AOS-hosted DEV (WSL background-job sessions in this repo, confirmed live 2026-08-21)** — the AOS Aspire host (`~/projects/aos/apphost.cs`, resource name drifts per AppHost run - `prism-epunygvd`, then `prism-bheqtamr` on 2026-08-26; `aspire resource prism restart` matched by prefix earlier that day but `prism.qa` had to read the real name from `aspire describe` - query it, never assume) runs `uv run --project services/prism-service ... prism start --ui-port 7780` directly against THIS checkout (`/home/siegeon/projects/prism`, `PRISM_DATA_DIR=/home/siegeon/.prism`). MCP `http://localhost:7777/mcp/?project=prism`, Web UI `http://localhost:7780/`. `PRISM_TASK_RUNNER_INTERVAL=900` and `PRISM_GATE_ADJUDICATOR_INTERVAL=60` are both set here, so the autonomous drive loop and machine gate adjudication are LIVE — a task flipped to `in_progress` in this project WILL be driven for real, unprompted. After any code change: sync the edited files into this checkout (worktree changes don't auto-appear here — `git checkout HEAD -- <path>` after merging to `dev`/`main`), then `aspire resource prism-epunygvd restart --apphost ~/projects/aos/apphost.cs --non-interactive` (falls back to `start` if `restart` errors "Failed to stop resource" — a known-flaky Aspire CLI path, not a real failure), then confirm `/api/version` reports the new build. This superseded the "DEV (what sessions in this repo use)" entry below for this machine at some point — the entry is left in place because it may still describe a genuinely separate Windows-side session context this WSL job never observed directly; verify live before assuming either is current.
- **DEV (Windows source-run, as previously documented)** — MCP `http://127.0.0.1:8887/mcp/?project=prism`, Web UI `http://127.0.0.1:8888/`. Claude Code's prism MCP for `E:\.prism` is overridden to 8887 in `~/.claude.json`; if it's unreachable, ask the owner to start dev (`prism-dev` skill) and `/mcp` reconnect — never build an HTTP shim around it. Use `127.0.0.1`, not `localhost` (IPv6-first resolution stalls ~200ms/request). Every code change ends with this daemon bounced and `/api/version` reporting the new build. NOT reachable from a WSL session (confirmed 2026-08-21: `curl` to 8887/8888 from WSL returns nothing) — this is a Windows-local process.
- **RELEASE (leave alone)** — WSL pipx: MCP `http://localhost:7777/mcp/?project=prism`, Web UI `http://localhost:7778/`. The same FastAPI process serves `/api/*` (JSON), `/sse/sessions` (events), and `/graph/viewer/{project}` (Sigma WebGL) on both instances. Default tool profile is `interactive`; use `tool_profile=all` for admin sessions. NOTE 2026-08-21: port 7778 was observed serving a `vite preview` STATIC BUILD of the web bundle, not a full FastAPI backend — the real backend for that frontend may be the AOS-hosted instance above, not a separate pipx install; this entry needs re-verification, not blind trust.

**Dev on this machine**: use the `prism-dev` skill — editable install from `E:\.prism\.venvs\dev`, source-run on 8887/8888, Edge `--app` window. Never docker/pipx/Tauri for local dev; any path >30s build/install is wrong. Patch-bump `PRISM_VERSION` on every user-visible change and bounce the daemon. For the AOS-hosted instance above, "bounce the daemon" means the `aspire resource ... restart`/`start` command, not this skill's own launch mechanism.

The `prism` CLI and `services/prism-service/docker-compose.yml` are end-user / server paths, never dev paths.

UI HMR runs on 5173 (`npm run dev` under `prism_service/web`) and hits the running API, not its own backend.
