<!-- PRISM repository index. Agent-first: this is the page an AI agent reads
     to install PRISM, wire up its MCP, and start working tasks. Keep the
     version line in sync with prism_service/__version__.py on each release. -->

<div align="center">

# PRISM

**An on-prem memory + knowledge layer and conductor for AI coding agents — over MCP.**

Durable project memory, a navigable knowledge wiki, a code graph, and a gated SDLC task conductor — so your agent builds with continuity instead of starting cold every session.

[![version](https://img.shields.io/badge/version-v6.7.3-6E7FD7)](./services/prism-service/prism_service/__version__.py)
[![python](https://img.shields.io/badge/python-3.12-3776AB)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-Apache--2.0-green)](#license)
[![MCP](https://img.shields.io/badge/MCP-for%20coding%20agents-555)](https://modelcontextprotocol.io/)

</div>

---

## All you need

```bash
pipx install prism-service     # 1. install (isolated; Python 3.12)
prism start --daemon           # 2. start the daemon — web :7778 · MCP :7777
```

```text
3. point your agent at the MCP, then call prism_onboard once:
   add { "mcpServers": { "prism": { "type": "http",
     "url": "http://localhost:7777/mcp/?project=<name>" } } } to its .mcp.json
   → prism_onboard auto-seeds architecture principles (so plan_gate passes) and
     returns the .mcp.json snippet, ports, version, and a prism_guide pointer.
4. call prism_guide first        → live orientation + the task playbook.
```

> **New here, AI agent?** Install, register the MCP, then **call `prism_guide` first**.

## What you get

- **Memory** — durable conventions, decisions, and failures with importance/decay, recalled into context so the agent stops repeating mistakes.
- **Knowledge wiki** — one interconnected **OKF / Understand** wiki over your curated memory: a concept graph you click through, with cross-links and backlinks.
- **Code graph** — hybrid retrieval (vector + BM25 + graph) via `brain_search` / `brain_understand` / `brain_call_chain`.
- **Gated conductor** — a hierarchical SDLC state machine: **epics → demonstrable-feature subtasks** with proof-carrying gates and epic → subtask fan-out.

## Benchmarks (comparative + honest)

Same harness, **LongMemEval R@5** (recall@5 — did the gold memory land in the top-5):

| Stack | R@5 | Scope |
|---|---|---|
| vanilla single-vector RAG **baseline** (potion-base-32M) | **0.524** | full 500 Q |
| + a stronger embedder (all-MiniLM-L6-v2) | **0.634** | full 500 Q |
| PRISM's current multi-granular + query-decomp stack | **0.94–0.98** | 50-Q smoke (pool@50 = 1.000) |

So a vanilla setup scores ~0.52; PRISM's retrieval pipeline lifts that to **0.94–0.98** on the smoke set. PRISM does **not yet** claim to beat the best public coding agents (SWE-bench Verified etc. are not yet officially scored). Full table, industry context, and caveats below.

---

<details>
<summary><b>Benchmarks — full table, industry context & honest caveats</b></summary>

Real, reproducible numbers. Sources: [`benchmarks/EXPERIMENTS.md`](./benchmarks/EXPERIMENTS.md) (the append-only Brain-retrieval log) and [`benchmarks/README.md`](./benchmarks/README.md) (the claim policy).

**Memory retrieval — LongMemEval R@5** (CPU-only; the lift is measured on one harness so each row is apples-to-apples with the one above it):

| Stack | R@5 | Scope |
|---|---|---|
| vanilla single-vector RAG baseline (potion-base-32M, RRF 3-index) | **0.524** | full 500 Q |
| swap embedder → all-MiniLM-L6-v2 (22M params) | **0.634** | full 500 Q |
| + multi-granular chunking + contextual prefix | **0.940** | 50 Q smoke |
| + rules-based query decomposition | **0.980** | 50 Q smoke (pool@50 = 1.000) |

**Context assembly** — the `contextpack` benchmark scores **1.000** across every dimension (persona frame, Brain/Memory/Task recall, no-noise/leakage, deterministic asset digests).

**Meta-conductor prompt-promotion** — decision_accuracy **1.000** with **0** false promotions (no-LLM candidate generation + holdout-gated promotion).

**Industry context (directional only — read the metric caveat).** Leading published LongMemEval memory systems sit in roughly the **0.82–0.97** band. But this is **directional, not a leaderboard**: recall@5 (did the gold memory appear in top-5) is **not** the same as the end-to-end **QA accuracy** many systems report, so cross-system numbers are directional only. As same-metric-family context, MemPalace reports recall_any@5 ≈ 96.6% ([mempalace.tech/benchmarks](https://www.mempalace.tech/benchmarks)); Mem0/Zep figures are typically QA accuracy ([state of AI agent memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026)). We do **not** present a "PRISM beats X" table.

**Honest scope caveat.** Our 0.94–0.98 is a **50-Q smoke**; the full-500-Q run of the top stack is **pending**. The tracked competitive coding-agent bars — **SWE-bench Verified**, **SWE-rebench**, **Terminal-Bench 2.0**, **BFCL V4** — are **not yet officially scored**, so public-best claims are blocked. The decisive next step is a paired **SWE-bench Lite** PRISM-on/off **proof campaign** (current local smoke is 1/2 vs 1/2 — too small to claim an advantage). Reproduce the numbers via `benchmarks/longmemeval/run.py` and `benchmarks/status/run.py`.

</details>

<details>
<summary><b>Working tasks the PRISM way (max fan-out + the MCP toolkit)</b></summary>

After install, from the agent: **call `prism_onboard` first** (one call seeds the default architecture principles so `plan_gate` is satisfiable, and returns the `.mcp.json` snippet, ports, version, and a `prism_guide` pointer). Then **call `prism_guide`** for the live orientation + the *"Working tasks the PRISM way"* playbook.

> Create an **epic** as a root task (tracked live on the conductor) → break it into demonstrable-feature **subtasks** → run independent subtasks **IN PARALLEL via subagents** → a **distinct-actor** subagent clears each red/green gate with real artifacts (no self-override, proof-carrying) → `principles_seed` so `plan_gate` passes → **roll the child proofs up** to the epic's `green_gate` → write the **why** back to memory at `green_gate` → browse what you know via the **OKF / Understand wiki** (`okf_index` / `okf_get` / `okf_graph`, `brain_understand`). Prefer the `implement` (build) and `prototype` (plan) workflows.

Core MCP tools: `prism_onboard`, `prism_guide`, `brain_search` / `brain_understand` / `brain_call_chain`, `okf_index` / `okf_get` / `okf_graph` (the Understand wiki), `principles_seed`, `memory_store` / `memory_recall`, `task_create` / `task_update` / `task_next`, `conductor_advance` / `conductor_gate`.

</details>

<details>
<summary><b>Install, ports, Web UI & run from source</b></summary>

**Native install** — PRISM is a pip/pipx package, `prism-service` (Python 3.12):

```bash
pipx install prism-service                    # isolated; recommended
prism start --daemon                          # web → :7778 · MCP → :7777
prism status                                  # confirm it's up
prism logs --follow                           # tail logs
curl http://localhost:7778/api/version        # → {"version": "6.7.3", ...}
```

Lifecycle: `prism start [--daemon]` · `prism status` · `prism logs` · `prism stop` · `prism update` · `prism version`.

**Web UI** — `http://localhost:7778/` — a React SPA: **Dashboard · Brain (graph) · Understand (wiki) · Tasks · Conductor · Sessions · Consolidation · Learning**. The footer shows the build + theme (e.g. *Slate Blue · v6.7.3*).

**Run from source (contributors):**

```bash
git clone https://github.com/siegeon/.prism && cd .prism
python -m venv .venv && . .venv/Scripts/activate           # 3.12
pip install -e services/prism-service
prism start --daemon
# SPA dev (HMR): cd services/prism-service/prism_service/web && npm install && npm run dev
```

See [`CLAUDE.md`](./CLAUDE.md) for repo conventions and layout, and the MCP `prism_guide` tool for the live agent playbook.

</details>

<details>
<summary><b>Status & license</b></summary>

Active development — `prism_service/__version__.py` is the canonical version + changelog (`PRISM_VERSION_NOTES`). Current: **v6.7.3** (README redesigned for human readability — progressive disclosure + comparative, honest benchmarks).

<a name="license"></a>**License:** Apache-2.0.

</details>
