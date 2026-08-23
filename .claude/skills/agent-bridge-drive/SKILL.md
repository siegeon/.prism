---
name: agent-bridge-drive
description: Drive a specific PRISM user's own already-open browser tab live via the agent_bridge_command MCP tool (navigate/click/fill/read/screenshot) -- never Playwright, never a separate/headless browser. Use when the user says "remote assist", "watch me work", hands you a bridge session id from Settings > Access key, or asks you to navigate/click/screenshot their live PRISM tab. Route the actual driving to a cheap/fast subagent (haiku tier) once you have a session id -- don't hand-roll this inline in the main thread every time.
version: 1.0.0
---

# Agent bridge — drive a live PRISM tab

## What this is

A logged-in PRISM user enables "Remote assist" in Settings > Access key,
which mints a short-lived session id tied to THAT tab. An authorized agent
(holding that user's PRISM access key) can then send structured commands
into that exact tab over SSE — the person watches their own screen react
live. There is no separate browser anywhere in this design; if you ever
reach for Playwright/agent-browser for this, you are doing it wrong.

## The one call you need

`agent_bridge_command` is an MCP tool on PRISM's own MCP server. Call it
with a raw JSON-RPC POST — this is more reliable than the `mcp` Python
SDK's `streamablehttp_client`, which has been observed to hang on async
cleanup after a successful call on this server's stateless transport
config. Don't fight that; just use curl:

```bash
curl -s -m 20 -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
        "name":"agent_bridge_command",
        "arguments":{"session_id":"<id>","action":"navigate","path":"/workflows"}
      }}' \
  "http://localhost:7777/mcp/?project=prism"
```

Swap `arguments` per action:
- `navigate`: `{"session_id":"<id>","action":"navigate","path":"/tasks"}`
- `click`/`read`: `{"session_id":"<id>","action":"click","selector":"<css selector>"}`
- `fill`: `{"session_id":"<id>","action":"fill","selector":"<css>","value":"<text>"}`
- `screenshot`: `{"session_id":"<id>","action":"screenshot","selector":"<css, optional>"}`
  — omit `selector` to capture the whole page, but PREFER scoping to the
  specific element you actually care about: it's faster, and this app's
  design system uses `oklch`/`oklab` colors extensively, which the
  rendering library (`html2canvas-pro`) mostly handles but a smaller,
  simpler DOM subtree is still safer and quicker to rasterize.

The response's `data.image_path` (screenshot only) is an ABSOLUTE LOCAL
FILE PATH under `PRISM_DATA_DIR/agent-bridge/<session_id>/` — read it
directly with your Read tool (it's a real PNG on this machine, not a URL).
Never ask for or embed the raw base64 image inline.

## Getting a session id

The owner enables Remote Assist in Settings > Access key and pastes you
the session id shown there. If a call fails with "no active bridge
session with that id owned by this caller", the id is dead (expired, the
owner turned the toggle off, or a daemon restart wiped it — bridge
sessions are deliberately in-memory only, never persisted). Ask for a
fresh one rather than retrying blind.

## Route the actual driving to a cheap/fast subagent

Once you have a session id, don't keep hand-rolling curl calls in the
main thread turn after turn — that's slow and burns the wrong tier of
model on pure mechanical work (owner, live: "we need to make sure we
dont re invent how to interact with the app... route... to cheap fast sub
agent to drive the app with agent assist"). Spawn a subagent on the
`haiku` model tier with the session id, the exact sequence of
navigate/click/fill/read/screenshot calls needed, and ask it to report
back what it observed (for screenshots: read the PNG and describe
concretely what's rendered, don't guess from source code). Keep the
main thread for deciding WHAT to check, not executing each mechanical
step.

## Known failure modes (don't re-diagnose these from scratch)

- **"unknown action: X"** — this error string comes from the BROWSER's
  own JS (`agentBridge.tsx`'s catch-all), not the server. It means the
  tab is running a JS bundle older than whatever action you're calling —
  the owner needs a genuine cache-bypassing reload (Ctrl+Shift+R / close
  and reopen the tab), not just Ctrl+R. A stale `index.html` pinning an
  old JS hash is a real, previously-seen failure mode here; if `npm run
  build` was run without first `rm -rf web_dist` (Vite doesn't auto-clean
  an outDir outside the project root), stale hashed chunks can survive
  a rebuild.
- **"no active bridge session with that id owned by this caller"** — the
  session is dead. Ask for a fresh one; don't retry the same id.
- **A call that should be instant instead hangs for ~20s with no
  response at all** — this is `agent_bridge_command`'s own
  `COMMAND_TIMEOUT_SECONDS=20.0` server-side wait for the browser's
  result; it means the tab never had a live listener (session stale, or
  Remote Assist was never actually re-enabled after a reload).
- **EVERY MCP tool call hangs, not just this one** (test with a
  completely unrelated tool like `prism_status` — if that hangs too,
  it's not an agent-bridge bug at all) — this has been caused, once
  observed live, by an unrelated large SQLite file (`brain.db`, ~2.5GB)
  being cold on a freshly-moved/relocated drive, stalling a background
  worker's disk I/O and exhausting the server's default thread pool
  (every MCP tool dispatch runs via `asyncio.to_thread`, a shared, fixed-
  size pool). Confirm the theory with `time cat <the db file> > /dev/null`
  before assuming a code bug — if that read is slow, warming the OS page
  cache resolves it without touching any code.
