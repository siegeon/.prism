# prism-service

PRISM — a software-engineering methodology + MCP service for AI-assisted
development. Hybrid SQLite-vec + BM25 + graph knowledge base, MCP tools for
Claude Code, governance / drift / quality background loops, and a React UI.

## Install

```bash
pipx install prism-service     # isolated CLI (recommended)
# or
pip install prism-service      # into the current env
```

Python 3.10+. Brings in `torch` (CPU build) and `sentence-transformers` for
local embeddings — no API tokens required for the core search path.

## Run

```bash
prism start                    # foreground
prism start --daemon           # background (writes pid + log into data dir)
prism status
prism logs --follow
prism stop
prism update
```

Default ports: UI on `http://localhost:7778/`, MCP on `http://localhost:7777/mcp/`.

## Data directory

Auto-resolved per platform:

| Platform | Path |
| --- | --- |
| Windows | `%LOCALAPPDATA%\prism\` |
| macOS / Linux | `~/.prism/` |
| Docker image | `/data` (legacy; still honored when present) |
| explicit override | `$PRISM_DATA_DIR` |

## Docker

The docker image is still supported for server / CI deploys. The image
itself uses `pip install .` under the hood, so the docker and pip paths
share one install codepath:

```bash
cd services/prism-service && docker compose up -d
```

## Source

Code, methodology, and the React/Vite SPA live at
<https://github.com/siegeon/.prism>.
