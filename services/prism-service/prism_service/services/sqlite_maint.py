"""Periodic SQLite maintenance — WAL checkpointing + query-planner stats.

sqlite-hardening workstream: the audit found NO wal_checkpoint/optimize
anywhere in the service, with live symptoms to match — tasks.db's main
file a week stale (all writes stranded in the -wal), scores.db's -wal
3.3x the size of the db, recall_log's -wal 8x. Long-lived readers keep
the WAL from self-checkpointing, so we do it explicitly on a cadence.

Every ~PRISM_SQLITE_MAINT_INTERVAL_S seconds (default 900, 0=off) and
once more on graceful shutdown, each known per-project store gets a
short-lived connection that runs:

    PRAGMA wal_checkpoint(TRUNCATE);  -- fold -wal back into the db
    PRAGMA optimize;                   -- refresh query-planner stats

Exceptions are logged, never raised. Deliberately NO VACUUM — that is
an operator-run job (it rewrites the whole file and takes an exclusive
lock for the duration).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 900  # 15 min

# Known per-project stores, relative to the project data dir.
_STORE_RELPATHS = (
    "tasks.db",
    "scores.db",
    "brain.db",
    "graph.db",
    "mulch/recall_log.db",
)


def maint_interval_s() -> float:
    """Read PRISM_SQLITE_MAINT_INTERVAL_S (seconds; 0 or negative = off)."""
    raw = os.environ.get("PRISM_SQLITE_MAINT_INTERVAL_S", "")
    if not raw.strip():
        return float(DEFAULT_INTERVAL_S)
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "invalid PRISM_SQLITE_MAINT_INTERVAL_S=%r; using default %ss",
            raw, DEFAULT_INTERVAL_S)
        return float(DEFAULT_INTERVAL_S)


def checkpoint_db(path: str | Path) -> bool:
    """Checkpoint + optimize ONE db file on a short-lived connection.

    Returns True on success, False when the file is missing or the
    pragmas failed (logged, never raised)."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        conn = sqlite3.connect(str(p), timeout=5.0)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA optimize")
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 — maintenance must never raise
        logger.warning("sqlite maintenance failed for %s: %s", p, exc)
        return False


def run_sqlite_maintenance() -> int:
    """One maintenance pass over every known store of every project.

    Returns the number of stores successfully checkpointed. Fully
    best-effort: a failing project or store is logged and skipped."""
    done = 0
    try:
        from prism_service.project_context import get_all_projects, get_project
        projects = get_all_projects() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("sqlite maintenance: project enumeration failed: %s", exc)
        return 0
    for pid in projects:
        try:
            data_dir = get_project(pid)._data_dir
        except Exception as exc:  # noqa: BLE001
            logger.warning("sqlite maintenance: skipping %s: %s", pid, exc)
            continue
        for rel in _STORE_RELPATHS:
            if checkpoint_db(Path(data_dir) / rel):
                done += 1
    logger.info("sqlite maintenance pass: %d store(s) checkpointed", done)
    return done


async def _maintenance_loop(interval_s: float) -> None:
    """Sleep-first loop: the boot itself isn't a checkpointing moment
    (the shutdown pass covers short-lived processes)."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            # Pragmas are quick but they do file I/O — keep the event
            # loop free by running the pass on a worker thread.
            await asyncio.to_thread(run_sqlite_maintenance)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.warning("sqlite maintenance pass errored", exc_info=True)


def start_sqlite_maintenance() -> "asyncio.Task | None":
    """Start the periodic maintenance loop on the running event loop.

    Called from main.py's lifespan (async context). Returns the task so
    the caller can cancel it on shutdown, or None when disabled via
    PRISM_SQLITE_MAINT_INTERVAL_S=0."""
    interval = maint_interval_s()
    if interval <= 0:
        logger.info(
            "sqlite maintenance disabled (PRISM_SQLITE_MAINT_INTERVAL_S=0)")
        return None
    logger.info("sqlite maintenance running every %.0fs", interval)
    return asyncio.get_running_loop().create_task(
        _maintenance_loop(interval), name="sqlite-maintenance")
