<!-- PRISM repository index. Agent-first: this is the page an AI agent reads
     to install PRISM, wire up its MCP, and start working tasks. Keep the
     version line in sync with prism_service/__version__.py on each release. -->

# PRISM

**Slate Blue · v6.7.0** — an on-prem **memory + knowledge layer and conductor for AI coding agents**, exposed over MCP. PRISM gives an agent durable project memory, a navigable knowledge wiki, a code graph, and a gated SDLC task conductor — so the agent (and the humans beside it) build with continuity instead of starting cold every session.

> New here? If you are an AI agent: install PRISM, register its MCP, then **call `prism_guide` first** — it returns the live orientation and the task-orchestration playbook.

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
curl http://localhost:7778/api/version      # → {"version": "6.7.0", ...}
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

Then, from the agent: **call `prism_guide` first**. It returns what each tool does, the two-surface knowledge model, and the *"Working tasks the PRISM way"* playbook:

> Create an **epic** as a root task (tracked live on the conductor) → break it into demonstrable-feature **subtasks** → drive each through the SDLC gates → **fan out subagents** (build with one, verify gates with a *distinct* actor — no self-override, proof-carrying) → write the **why** back to memory at `green_gate`. Prefer the `implement` (build) and `prototype` (plan) workflows.

Core MCP tools: `prism_guide`, `brain_search` / `brain_understand` / `brain_call_chain`, `okf_index` / `okf_get` / `okf_graph` (the Understand wiki), `memory_store` / `memory_recall`, `task_create` / `task_update` / `task_next`, `conductor_advance` / `conductor_gate`.

---

## Web UI

`http://localhost:7778/` — React SPA: **Dashboard · Brain (graph) · Understand (wiki) · Tasks · Conductor · Sessions · Consolidation · Learning**. The footer shows the build + theme (e.g. *Slate Blue · v6.7.0*).

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

Active development — `prism_service/__version__.py` is the canonical version + changelog (`PRISM_VERSION_NOTES`). Current: **v6.7.0** (hosted OKF knowledge wiki + Brain/Understand consolidation + renameable tasks + agent-orchestration playbook).
