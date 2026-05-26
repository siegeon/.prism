#!/usr/bin/env python3
"""PRISM Service — Brain/Memory/Tasks/Workflow with React SPA + MCP.

The React/Vite SPA is built at image-build time (or `npm run build` in
prism_service/web/) into prism_service/web_dist/ and served from /. The
/api/* JSON surface backs every page; /sse/sessions streams events;
/graph/viewer and /graphify-visual/* serve the standalone graph viewer
plus its data files.

MCP runs on a separate uvicorn started in a background thread on MCP_PORT.
"""

from __future__ import annotations

import sys as _sys
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from prism_service.config import (
    DATA_DIR, PROJECT_DIR, PROJECTS_DIR,
    UI_PORT, MCP_PORT,
    GOVERNANCE_INTERVAL_SECONDS, DRIFT_INTERVAL_SECONDS, QUALITY_INTERVAL_SECONDS,
)

PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
_LOCK_FILE = DATA_DIR / ".mcp_started"
WEB_DIST = Path(__file__).parent / "web_dist"


def start_mcp_server():
    """Start MCP server in background thread."""
    from prism_service.mcp.server import run_mcp_server
    run_mcp_server(MCP_PORT)


def start_governance_timer():
    """Run governance cycles for every project on a cadence."""
    from prism_service.project_context import get_project, get_all_projects
    while True:
        try:
            for pid in get_all_projects():
                try:
                    get_project(pid).governance.run_cycle()
                except Exception as e:
                    print(f"Governance cycle error ({pid}): {e}")
        except Exception as e:
            print(f"Governance timer error: {e}")
        time.sleep(GOVERNANCE_INTERVAL_SECONDS)


def start_drift_timer():
    """Reindex drifted Brain docs per project on a cadence.

    Uses a dedicated Brain per project (separate SQLite connections from
    the request path) so a long reindex transaction doesn't park MCP
    workers behind the same connection mutex — same fix as issue #38.
    """
    if DRIFT_INTERVAL_SECONDS <= 0:
        print("Drift timer disabled (PRISM_DRIFT_INTERVAL=0)", file=_sys.stderr)
        return
    from prism_service.project_context import get_project, get_all_projects
    from prism_service.engines.brain_engine import Brain
    print(f"Drift timer running every {DRIFT_INTERVAL_SECONDS}s", file=_sys.stderr)
    drift_brains: dict[str, Brain] = {}
    # Tight loop checks for soft-deleted projects every STALE_CHECK_S so
    # cached Brain SQLite handles release within seconds of a DELETE —
    # the trash sweeper can then rmtree the .db files. Reindex remains
    # on the configured DRIFT_INTERVAL_SECONDS cadence.
    STALE_CHECK_S = 5
    last_reindex = 0.0
    while True:
        try:
            live = set(get_all_projects())
            for stale_pid in [p for p in drift_brains if p not in live]:
                stale_brain = drift_brains.pop(stale_pid, None)
                for close_name in ("close", "shutdown"):
                    close = getattr(stale_brain, close_name, None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
                        break
                print(f"[drift] released stale Brain for {stale_pid}", file=_sys.stderr)

            now = time.time()
            do_reindex = now - last_reindex >= DRIFT_INTERVAL_SECONDS
            if not do_reindex:
                time.sleep(STALE_CHECK_S)
                continue
            last_reindex = now

            for pid in live:
                try:
                    ctx = get_project(pid)
                    db_dir = ctx._data_dir
                    brain = drift_brains.get(pid)
                    if brain is None:
                        brain = Brain(
                            brain_db=str(db_dir / "brain.db"),
                            graph_db=str(db_dir / "graph.db"),
                            scores_db=str(db_dir / "scores.db"),
                            tasks_db=str(db_dir / "tasks.db"),
                        )
                        drift_brains[pid] = brain
                    n = brain.incremental_reindex()
                    if n:
                        print(f"[drift] {pid}: reindexed {n} drifted file(s)", file=_sys.stderr)
                except Exception as e:
                    print(f"Drift cycle error ({pid}): {e}", file=_sys.stderr)
        except Exception as e:
            print(f"Drift timer error: {e}", file=_sys.stderr)
        time.sleep(STALE_CHECK_S)


def start_quality_timer():
    """Score merged tasks against git truth on a cadence (LL-04)."""
    if QUALITY_INTERVAL_SECONDS <= 0:
        print("Quality timer disabled (PRISM_QUALITY_INTERVAL=0)", file=_sys.stderr)
        return
    from prism_service.project_context import get_project, get_all_projects
    from prism_service.services.scoring_service import score_merged_tasks
    print(f"Quality timer running every {QUALITY_INTERVAL_SECONDS}s", file=_sys.stderr)
    while True:
        try:
            for pid in get_all_projects():
                try:
                    ctx = get_project(pid)
                    scored = score_merged_tasks(
                        tasks_svc=ctx.task_svc,
                        scores_db=str(ctx._data_dir / "scores.db"),
                        repo_path=str(PROJECT_DIR),
                    )
                    if scored:
                        print(f"[quality] {pid}: scored {len(scored)} merged task(s)", file=_sys.stderr)
                except Exception as e:
                    print(f"Quality cycle error ({pid}): {e}", file=_sys.stderr)
        except Exception as e:
            print(f"Quality timer error: {e}", file=_sys.stderr)
        time.sleep(QUALITY_INTERVAL_SECONDS)


def _install_stackdump_handler() -> None:
    """Dump every thread's stack to stderr on SIGUSR1 (preserved from #38)."""
    import signal
    if not hasattr(signal, "SIGUSR1"):
        return

    def _dump(_signum, _frame):
        frames = _sys._current_frames()
        out = [f"=== thread stack dump ({len(frames)} threads) ==="]
        thread_names = {t.ident: t.name for t in threading.enumerate()}
        for tid, frame in frames.items():
            name = thread_names.get(tid, "?")
            out.append(f"\n# Thread {tid} ({name})")
            out.append("".join(traceback.format_stack(frame)))
        out.append("=== end stack dump ===\n")
        print("\n".join(out), file=_sys.stderr, flush=True)

    try:
        signal.signal(signal.SIGUSR1, _dump)
    except (ValueError, OSError):
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start MCP + governance/drift/quality/drainer timers on lifespan boot.

    The lock file only exists to prevent double-start within one
    process. A fresh process — including the container Watchtower just
    swapped in — must always start its own threads. The old code
    treated any pre-existing lock as "already running" and silently
    skipped every background timer after a swap (drift, governance,
    quality, and the v5.1.6 understand_drainer). v5.1.7 reclaims a
    stale lock instead of bailing.
    """
    if _LOCK_FILE.exists():
        try:
            stale_tid = _LOCK_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            stale_tid = "?"
        print(
            f"[lifespan] stale lock detected (was tid={stale_tid}); "
            f"reclaiming for tid={threading.get_ident()}",
            file=_sys.stderr, flush=True,
        )
    try:
        _LOCK_FILE.write_text(str(threading.get_ident()))
        _install_stackdump_handler()
        threading.Thread(target=start_mcp_server, daemon=True).start()
        threading.Thread(target=start_governance_timer, daemon=True).start()
        threading.Thread(target=start_drift_timer, daemon=True).start()
        threading.Thread(target=start_quality_timer, daemon=True).start()
        from prism_service.services.understand_drainer import start_understand_drainer
        threading.Thread(target=start_understand_drainer, daemon=True).start()
        # v5.3.0 — sweep .trash/ (renamed-on-delete project dirs) until
        # the OS releases SQLite file locks the timer threads hold open.
        from prism_service.services.trash import start_trash_sweeper
        threading.Thread(target=start_trash_sweeper, daemon=True).start()
        # v6.0.1 — poll GitHub Releases for newer versions and auto-apply.
        # Skips itself on docker (Watchtower's domain) and respects
        # PRISM_AUTO_UPDATE=off / PRISM_AUTO_UPDATE_INTERVAL=0.
        from prism_service.services.auto_updater import start_auto_updater
        start_auto_updater()
        # v5.3.15 — read claude session transcripts directly from
        # ~/.claude/projects/<slug>/*.jsonl and populate session_outcomes.
        # Cuts the dependency on the Stop hook (which silently no-ops on
        # Windows when commands say `python3` — the MS Store stub eats
        # them) and backfills the user's historical sessions.
        from prism_service.services.claude_transcripts import start_transcript_importer
        threading.Thread(target=start_transcript_importer, daemon=True).start()
        # v6.0.18 — opt-in background reflection. Off by default (a
        # zero-LLM service shouldn't burn claude tokens unprompted);
        # set PRISM_REFLECTION_WORKER=on to drain pending briefs every
        # PRISM_REFLECTION_WORKER_INTERVAL seconds.
        from prism_service.services.reflection_worker import start_reflection_worker
        start_reflection_worker()
    except Exception as e:
        print(f"Startup error: {e}", file=_sys.stderr, flush=True)
    yield
    _LOCK_FILE.unlink(missing_ok=True)


app = FastAPI(title="PRISM Service", lifespan=lifespan)

# JSON API for the SPA + non-API routes (SSE, graph viewer).
from prism_service.api import api_router
from prism_service.routes import routes_router
app.include_router(api_router)
app.include_router(routes_router)


# Static SPA. /assets/* served directly; everything else falls through to
# index.html so client-side routing handles deep links.
_RESERVED_PREFIXES = ("api/", "sse/", "graph/", "graphify-visual/", "mcp/", "openapi.json", "docs", "redoc")

if WEB_DIST.exists():
    _assets_dir = WEB_DIST / "assets"
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    # index.html must never be cached — Vite builds hash every asset
    # in /assets/ so those are content-addressable and safe to cache
    # forever, but the SPA entry point is the indirection layer that
    # tells the browser which hash to fetch. WebView2 / Edge ignore
    # client-Ctrl-R reloads in some configs so we set headers explicitly
    # to prevent old HTML pinning the browser to a stale JS bundle.
    _NO_CACHE_HEADERS = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa_fallback(full_path: str):
        if any(full_path.startswith(p) for p in _RESERVED_PREFIXES):
            return JSONResponse({"detail": "not found"}, status_code=404)
        # Serve favicon/static-at-root files if they exist next to index.html
        candidate = WEB_DIST / full_path if full_path else WEB_DIST / "index.html"
        if full_path and candidate.exists() and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(
            str(WEB_DIST / "index.html"),
            headers=_NO_CACHE_HEADERS,
        )
else:
    @app.get("/", include_in_schema=False)
    def _no_spa():
        return JSONResponse(
            {"detail": "SPA build missing — run `npm run build` in prism_service/web/ or rebuild the image."},
            status_code=503,
        )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=UI_PORT)
