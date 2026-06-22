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

import atexit
import faulthandler
import logging
import os
import signal
import sys as _sys
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

# GH #155 — pin native-math thread pools to 1 BEFORE any numpy/model2vec
# import. The config/brain import block below transitively pulls numpy in,
# whose BLAS backends read these vars once at load — so this MUST run first
# (operator-set values are left untouched). This is the PREVENT layer of the
# silent-all-threads-wedge fix.
from prism_service.thread_limits import apply_thread_limits
apply_thread_limits()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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

_log = logging.getLogger("prism")
_logging_configured = False


def _configure_logging() -> None:
    """Capture logs + crashes for every launch path (issue #66).

    The daemon's stdout/stderr are redirected to prism.log by the CLI
    (and by service_entry), so all the existing print()s and uvicorn's
    default stderr logging already land in the file. This adds, IN
    PROCESS so it holds on EVERY path (foreground/daemon/pipx/Tauri):
      * rotation-on-start (bounded size, prior run preserved),
      * faulthandler (a C-level traceback even when the interpreter is
        too wedged to run Python — the silent-exit case),
      * sys/threading excepthooks that LOG the full stack instead of
        letting an uncaught exception die with only a bare print.
    Deliberately does NOT reassign sys.stdout/stderr (that would break
    pytest capture and isn't needed — the fd redirect already routes
    them to the file). Idempotent.
    """
    global _logging_configured
    if _logging_configured:
        return
    from prism_service.data_dir import log_file, rotate_log_on_start
    rotate_log_on_start(log_file())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # basicConfig is a no-op if the root logger already has handlers, so
    # set the level explicitly to guarantee INFO records surface.
    logging.getLogger().setLevel(logging.INFO)
    try:
        faulthandler.enable()  # defaults to sys.stderr -> prism.log on the daemon
    except (OSError, ValueError):
        pass

    def _log_uncaught(exc_type, exc_value, exc_tb):
        _log.critical("uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

    _sys.excepthook = _log_uncaught
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda a: _log.critical(
            "uncaught thread exception",
            exc_info=(a.exc_type, a.exc_value, a.exc_traceback),
        )
    _logging_configured = True


def _own_pidfile_cleanup() -> None:
    """Unlink the pidfile only if it still names THIS process, so the
    daemon clears its own pidfile on graceful exit (issue #66 fix #2)."""
    from prism_service.data_dir import pid_file
    p = pid_file()
    try:
        if p.exists() and p.read_text(encoding="utf-8").strip() == str(os.getpid()):
            p.unlink(missing_ok=True)
    except OSError:
        pass


def _install_mode() -> str:
    """Classify how this copy was installed so the SPA-missing 503 can
    suggest a remediation the user can actually run (issue #66)."""
    try:
        from prism_service.services.auto_updater import _running_in_docker
        if _running_in_docker():
            return "docker"
    except Exception:
        pass
    # A sibling web/ source tree means this is a source / editable
    # checkout where `npm run build` is meaningful.
    if (Path(__file__).parent / "web").exists():
        return "source"
    return "pipx"


def _spa_missing_payload() -> dict:
    """The install-mode-aware 503 body for when web_dist/ is absent (#66)."""
    mode = _install_mode()
    if mode == "source":
        remediation = "run `npm run build` in prism_service/web/"
    elif mode == "docker":
        remediation = "rebuild the image (the SPA is built at image-build time)"
    else:
        # pipx / pip install whose wheel shipped without web_dist/ — the
        # source-only `npm` hint is unreachable for these users.
        remediation = "run `prism update` (or `pip install --upgrade prism-service`)"
    return {
        "detail": f"SPA build missing — {remediation}.",
        "install_mode": mode,
        "remediation": remediation,
    }


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
                # FIX 4a — bounded retention sweep on the governance
                # cadence: prune old completed candidates + cap stale
                # session_outcomes so the queue/log can't grow forever.
                try:
                    from prism_service.services.consolidation_data import (
                        retention_sweep,
                    )
                    scores_db = str(get_project(pid)._data_dir / "scores.db")
                    rep = retention_sweep(scores_db)
                    if rep.get("candidates_pruned") or rep.get(
                        "session_outcomes_pruned"
                    ):
                        print(f"[retention] {pid}: pruned {rep}")
                except Exception as e:
                    print(f"Retention sweep error ({pid}): {e}")
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
    # Idempotent — also covers the PyInstaller/Tauri service_entry path
    # that boots uvicorn directly and never hits the __main__ block (#66).
    _configure_logging()
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
        threading.Thread(target=start_drift_timer, daemon=True).start()
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
        # Phase 3 (epic 4fd1e6b4) — TIMERS RETIRED ONTO THE BUS. The
        # Reflection Worker and Memory Summary Worker no longer run on their
        # own polling clocks; session.imported reflects (coalesced, read-
        # before-write) and memory.written summarizes (haiku) on the event
        # pool below. Their dual-run flags are gone with the spawn calls.
        # TRANSCRIPT_CANDIDATE_RETIRED_TO_BUS: the transcript->candidate
        # always-on reflection drain is retired — the transcript importer
        # still produces the candidate + emits SESSION_IMPORTED, and the
        # session.imported handler reflects on it via the bus.
        # Phase 4 (epic 4fd1e6b4) — the genuinely WALL-CLOCK memory duties
        # are now folded into ONE maintenance/heartbeat clock instead of 4-5
        # independent daemon threads. Each tick iterates every project once
        # and runs, in sequence, the five memory passes behind their OWN
        # cadence gates: governance (TTL+decay+dup-detect + retention sweep),
        # verify_staleness, forget, adaptive-policy retune, and quality-vs-git.
        # All prior env gates / cadence overrides are honored
        # (PRISM_GOVERNANCE_INTERVAL, PRISM_QUALITY_INTERVAL,
        # PRISM_ADAPTIVE_POLICY_WORKER[_INTERVAL], PRISM_<OP>_WORKER). The old
        # per-duty spawns are RETIRED from the lifespan: the governance memory
        # passes, the memory-ops VerifyStaleness/Forget fleet, the Tier-3
        # adaptive-policy worker, and the quality timer no longer run their own
        # threads. (drift_timer + the event pool stay separate, out of scope.)
        # MERGE_RETIRED_TO_BUS (Phase 3) still holds — the Memory Ops Merge op
        # remains retired onto the memory.written bus handler and is NOT one of
        # the five wall-clock passes the maintenance clock folds.
        from prism_service.services.maintenance_clock import start_maintenance_clock
        start_maintenance_clock()

        # GH #155 — deadlock watchdog. Arms faulthandler.dump_traceback_later
        # each cycle then runs a real HTTP self-probe; a wedge lets the
        # C-level timer fire and dump all thread stacks even though every
        # Python thread is deadlocked. Default ON (PRISM_WATCHDOG=off to
        # disable); opt-in os._exit(1) self-heal via PRISM_WATCHDOG_KILL=1.
        from prism_service.services.watchdog import start_watchdog
        start_watchdog()

        # Ultimate Graph narrative layer (#50) — names the code hierarchy
        # (domain/service/module) with inference, escaping scopes whose
        # files haven't changed. Defaults ON; PRISM_GRAPH_ENRICH_WORKER=off.
        from prism_service.services.graph_enrich import start_graph_enrich_worker
        start_graph_enrich_worker()
        # Phase 1 of epic 4fd1e6b4 — in-process event bus + concurrency-
        # capped, budget-governed consumer pool. SUBSTRATE only this phase:
        # handlers are wrap/no-op, no emitter is wired live, nothing shells
        # `claude -p`. The pool will become the single inference chokepoint
        # in Phases 2-3. Disable via PRISM_EVENT_POOL_INTERVAL=0.
        from prism_service.services.event_pool import start_event_pool
        start_event_pool()
    except Exception:
        # Issue #66: this used to `print(e)` and swallow the stack, so a
        # half-broken boot left no traceback. Log the full stack to the
        # rotated prism.log. Deliberately do NOT re-raise — keep the
        # current limp-along behavior; visibility is the fix here.
        _log.critical("lifespan startup failed", exc_info=True)
    yield
    try:
        from prism_service.services.watchdog import stop_watchdog
        stop_watchdog()
    except Exception:
        _log.warning("watchdog stop failed", exc_info=True)
    _LOCK_FILE.unlink(missing_ok=True)


app = FastAPI(title="PRISM Service", lifespan=lifespan)

# The standalone Tauri shell loads its splash from a tauri:// origin and
# polls /api/version cross-origin. Without these headers the webview sends
# the request (server logs it, returns 200) but the browser silently drops
# the response body — splash hangs at "Starting backend…" for 30s and
# falls through to a misleading "install via pipx" error (issue #88).
# Loopback-only socket so no allow_credentials needed.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(tauri://localhost|https?://tauri\.localhost)$",
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        return JSONResponse(_spa_missing_payload(), status_code=503)


if __name__ == "__main__":
    _configure_logging()
    # The daemon is the authority that clears its own pidfile on a
    # graceful exit (issue #66 fix #2) — atexit covers normal exit and
    # SIGTERM/SIGINT cover signalled shutdown. _own_pidfile_cleanup only
    # unlinks if the file still names this PID, so it never deletes a
    # newer daemon's pidfile.
    atexit.register(_own_pidfile_cleanup)

    def _on_signal(_signum, _frame):
        _own_pidfile_cleanup()
        raise SystemExit(0)

    for _sig_name in ("SIGTERM", "SIGINT"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is not None:
            try:
                signal.signal(_sig, _on_signal)
            except (ValueError, OSError):
                pass  # not in main thread / unsupported on this platform
    # Build an explicit Config+Server (instead of uvicorn.run) so we hold
    # the Server handle. The auto-updater's daemon thread can then REQUEST
    # a restart (a flag, never an in-thread execv — #66), and a watcher
    # flips server.should_exit so uvicorn drains the loop + closes sockets.
    # Once .run() returns (sockets fully closed) we os.execv from THIS main
    # thread to flip the served version. Supervisor-agnostic: bare daemon
    # re-execs in place; an external supervisor respawns on the clean exit.
    from prism_service.services import auto_updater as _au

    _config = uvicorn.Config(app, host="0.0.0.0", port=UI_PORT)
    _server = uvicorn.Server(_config)

    def _restart_watcher() -> None:
        # Poll the daemon-thread-set flag; when set, ask uvicorn to drain
        # and stop. The actual os.execv happens on the main thread AFTER
        # _server.run() returns, so live sockets are closed first.
        while not _server.should_exit:
            if _au.restart_requested():
                _log.info("auto-update restart requested; draining uvicorn")
                _server.should_exit = True
                return
            time.sleep(2.0)

    threading.Thread(
        target=_restart_watcher, daemon=True, name="prism-restart-watcher",
    ).start()

    try:
        _server.run()
    except Exception:
        # A bind failure or uvicorn-internal crash used to leave no
        # record (issue #66). Guarantee a traceback in prism.log.
        _log.critical("uvicorn exited abnormally", exc_info=True)
        raise

    # Reached only after a graceful stop. If the stop was triggered by an
    # auto-update restart request, re-exec the upgraded image from the MAIN
    # thread now that the sockets are closed (#66-safe). Otherwise this is a
    # normal shutdown and we just fall through to exit.
    if _au.restart_requested():
        # Do NOT unlink the pidfile here: os.execv replaces the process
        # image IN PLACE (same PID, no fork), so the pidfile that names
        # this PID stays valid across the re-exec — and the re-exec'd
        # `-m prism_service.main` never re-writes it (only `prism start`
        # does). Clearing it would leave a live daemon (same PID, new
        # version) with no pidfile, so `prism status`/`prism stop` would
        # report it as not running. atexit's cleanup does not fire on
        # os.execv either, so the file correctly survives the flip.
        _log.info("uvicorn stopped; performing main-thread re-exec (auto-update)")
        _au.perform_restart()
