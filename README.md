<!-- PRISM repository index. Agent-first: this is the page an AI agent reads
     to install PRISM, wire up its MCP, and start working tasks. Keep the
     version line in sync with prism_service/__version__.py on each release. -->

# PRISM

**Slate Blue · v6.7.2** — an on-prem **memory + knowledge layer and conductor for AI coding agents**, exposed over MCP. PRISM gives an agent durable project memory, a navigable knowledge wiki, a code graph, and a gated SDLC task conductor — so the agent (and the humans beside it) build with continuity instead of starting cold every session.

> New here? If you are an AI agent: install PRISM, register its MCP, then **call `prism_guide` first** — it returns the live orientation and the task-orchestration playbook.

---

## All you need

```bash
pipx install prism-service     # 1. install (isolated; Python 3.12)
prism start --daemon           # 2. start the daemon — web :7778 · MCP :7777
```

Then point your agent at the MCP and bootstrap in one call:

```json
// 3a. add to your agent's .mcp.json  ·  `project` namespaces a workspace
{ "mcpServers": { "prism": { "type": "http", "url": "http://localhost:7777/mcp/?project=<name>" } } }
```

```text
3b. from the agent, call the prism_onboard MCP tool   → auto-seeds default architecture
    principles (so plan_gate is satisfiable) AND returns the .mcp.json snippet, ports,
    version, and a "call prism_guide first" pointer.  prism_onboard does step 3a for you.
4.  call prism_guide first                             → live orientation + task playbook.
```

**→ you get:** durable cross-session **memory**, a navigable **OKF / Understand knowledge wiki**, a **code graph** (`brain_search` / `brain_understand` / `brain_call_chain`), and a **gated conductor SDLC** with epic → subtask fan-out.

See [**Benchmarks**](#benchmarks--what-youre-dealing-with) for the real numbers. Everything below is the detail behind these four steps.

---

## What PRISM gives an agent

- **Knowledge — two surfaces, one model.** *Brain* is the code-graph visualization + hybrid retrieval (vector + BM25 + graph). *Understand* is one interconnected **wiki** over your curated memory, expressed in the **Open Knowledge Format (OKF)**: a concept graph you click through, read, and follow cross-links + backlinks. (Memory and OKF are the same knowledge under the hood — not separate places.)
- **Memory.** Durable conventions, decisions, and failures with importance/decay/effectiveness — recalled into context so the agent stops repeating mistakes.
- **Conductor.** A hierarchical, gated SDLC state machine: epics → demonstrable-feature subtasks, each driven through story → plan → red → implement → green gates with proof-carrying verification.

---

## Quick start (native install)

PRISM is a pip/pipx package, `prism-service` (Python 3.12).

```bash
# 1. Install (isolated; recommended)
pipx install prism-service

# 2. Start the daemon (web UI + MCP)
prism start --daemon          # web → http://localhost:7778/  ·  MCP → http://localhost:7777/mcp/
prism status                  # confirm it's up
prism logs --follow           # tail logs

# 3. Verify it's running
curl http://localhost:7778/api/version      # → {"version": "6.7.2", ...}
```

Lifecycle: `prism start [--daemon]` · `prism status` · `prism logs` · `prism stop` · `prism update` · `prism version`.

---

## Wire up the MCP (so an agent can call PRISM)

Add PRISM to your agent's MCP config (e.g. `.mcp.json`) — it's a streamable-HTTP server. `project` namespaces a workspace:

```json
{
  "mcpServers": {
    "prism": { "type": "http", "url": "http://localhost:7777/mcp/?project=myproject" }
  }
}
```

Then, from the agent: **call `prism_onboard` first** — one call seeds the default architecture principles (so the conductor's `plan_gate` is satisfiable) and returns this `.mcp.json` snippet, the web/MCP ports, the running version, and a pointer to `prism_guide`. Then **call `prism_guide`** for the live orientation + the *"Working tasks the PRISM way"* playbook.

### Working tasks (max fan-out)

> Create an **epic** as a root task (tracked live on the conductor) → break it into demonstrable-feature **subtasks** → run independent subtasks **IN PARALLEL via subagents** → a **distinct-actor** subagent clears each red/green gate with real artifacts (no self-override, proof-carrying) → `principles_seed` so `plan_gate` passes → **roll the child proofs up** to the epic's `green_gate` → write the **why** back to memory at `green_gate` → browse what you know via the **OKF / Understand wiki** (`okf_index` / `okf_get` / `okf_graph`, `brain_understand`). Prefer the `implement` (build) and `prototype` (plan) workflows.

Core MCP tools: `prism_onboard`, `prism_guide`, `brain_search` / `brain_understand` / `brain_call_chain`, `okf_index` / `okf_get` / `okf_graph` (the Understand wiki), `principles_seed`, `memory_store` / `memory_recall`, `task_create` / `task_update` / `task_next`, `conductor_advance` / `conductor_gate`.

---

## Benchmarks — what you're dealing with

Real, reproducible numbers. No hype beyond the data. Sources: [`benchmarks/EXPERIMENTS.md`](./benchmarks/EXPERIMENTS.md) (the append-only Brain-retrieval log) and [`benchmarks/README.md`](./benchmarks/README.md) (the claim policy).

**Memory retrieval — LongMemEval R@5** (CPU-only default embedder, all-MiniLM-L6-v2, 22M params):

| Stack | R@5 | Scope |
|---|---|---|
| potion-base-32M baseline (RRF 3-index) | **0.524** | full 500 Q |
| swap embedder → all-MiniLM-L6-v2 | **0.634** | full 500 Q |
| + multi-granular chunking + contextual prefix | **0.940** | 50 Q smoke |
| + rules-based query decomposition | **0.980** | 50 Q smoke (pool@50 = 1.000) |

**Context assembly** — `contextpack` benchmark scores **1.000** across every dimension (persona frame, Brain/Memory/Task recall, no-noise/leakage, deterministic asset digests).

**Meta-conductor prompt-promotion** — decision_accuracy **1.000** with **0** false promotions (no-LLM candidate generation + holdout-gated promotion).

> **Honest caveat.** PRISM does **not yet** claim to beat the best public coding agents. The tracked competitive bars — **SWE-bench Verified**, **SWE-rebench**, **Terminal-Bench 2.0**, **BFCL V4** — are **not yet officially scored**, so public-best claims are blocked. The decisive next step is a paired **SWE-bench Lite** PRISM-on/off **proof campaign** (current local smoke is 1/2 vs 1/2 — too small to claim an advantage). Reproduce the numbers above via `benchmarks/longmemeval/run.py` and `benchmarks/status/run.py`.

---

## Web UI

`http://localhost:7778/` — React SPA: **Dashboard · Brain (graph) · Understand (wiki) · Tasks · Conductor · Sessions · Consolidation · Learning**. The footer shows the build + theme (e.g. *Slate Blue · v6.7.2*).

---

## Run from source (contributors)

```bash
git clone https://github.com/siegeon/.prism && cd .prism
python -m venv .venv && . .venv/Scripts/activate        # 3.12
pip install -e services/prism-service
prism start --daemon
# SPA dev (HMR): cd services/prism-service/prism_service/web && npm install && npm run dev
```

See [`CLAUDE.md`](./CLAUDE.md) for repo conventions and layout, and the MCP `prism_guide` tool for the live agent playbook.

---

## Status

Active development — `prism_service/__version__.py` is the canonical version + changelog (`PRISM_VERSION_NOTES`). Current: **v6.7.2** (README value-prop onboarding + honest benchmark numbers; hosted OKF knowledge wiki + Brain/Understand consolidation + agent-orchestration playbook).
