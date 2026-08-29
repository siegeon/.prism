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
from prism_service.services import sqlite_db
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 900  # 15 min

# Pages of FTS5 segment data merged per pass. 16 is SQLite's own
# documented incremental value: enough to keep up with a drift loop,
# small enough that one pass never stalls the maintenance thread.
_FTS_MERGE_PAGES = 16


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
        conn = sqlite_db.connect(str(p), timeout=5.0)
        try:
            # PRAGMA optimize runs ANALYZE-like queries that can write to
            # sqlite_stat tables, which re-grows the WAL — so it must run
            # BEFORE the checkpoint, never after, or the TRUNCATE's fold
            # gets undone by optimize's own write (measured: a 0-byte
            # -wal after TRUNCATE grew back to 12392 bytes post-optimize).
            conn.execute("PRAGMA optimize")
            # Bounded FTS5 segment merge, for the same reason and in the
            # same position as PRAGMA optimize: it WRITES, so it has to
            # run before the checkpoint or its own write undoes the fold.
            # Without it docs_fts segments accumulate forever (measured:
            # 306,959 segment rows and a 1.7 GB freelist for 3.5 MB of
            # text). The bounded VALUES('merge', N) form on purpose --
            # VALUES('optimize') rewrites the entire index every pass.
            try:
                conn.execute(
                    "INSERT INTO docs_fts(docs_fts, rank) "
                    "VALUES('merge', ?)", (_FTS_MERGE_PAGES,))
                conn.commit()
            except sqlite3.Error:
                # Most per-project stores carry no FTS index at all.
                pass
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
        # (busy, log_pages, checkpointed_pages). busy=1 means a reader
        # held a snapshot, the TRUNCATE was downgraded, and the -wal kept
        # its size -- reporting that as success is how a store grows a
        # 750 MB -wal while every maintenance pass logs a clean run.
        if row is not None and row[0]:
            logger.warning(
                "sqlite maintenance: checkpoint of %s was blocked by a live "
                "reader (busy=%s, wal=%s pages, folded=%s pages); the -wal "
                "was not truncated", p, row[0], row[1], row[2])
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — maintenance must never raise
        logger.warning("sqlite maintenance failed for %s: %s", p, exc)
        return False


def checkpoint_data_dir(data_dir: str | Path) -> int:
    """Discover + checkpoint every ``*.db`` under ``data_dir`` recursively.

    Replaces the old hardcoded relpath tuple: a recursive glob covers
    ``mulch/`` AND any FUTURE subdir, so a newly-added store can never
    silently bloat its ``-wal`` forever — the exact regression the
    v6.7.24 hardcoded list left open. Best-effort: a bad file is logged
    and skipped, and a glob failure returns 0 rather than raising."""
    root = Path(data_dir)
    try:
        dbs = sorted(root.glob("**/*.db"))
    except Exception as exc:  # noqa: BLE001 — maintenance must never raise
        logger.warning("sqlite maintenance: glob failed under %s: %s", root, exc)
        return 0
    return sum(1 for db in dbs if checkpoint_db(db))


def run_sqlite_maintenance() -> int:
    """One maintenance pass over every known store of every project.

    Enumerates project data dirs on the FILESYSTEM (config.list_projects
    + PROJECTS_DIR) — deliberately NOT via project_context.get_project,
    which would construct a full ProjectContext (Brain init, embedder
    load) per project under the shared registry lock just to checkpoint
    a file. Returns the number of stores successfully checkpointed.
    Fully best-effort: a failing project or store is logged and skipped."""
    done = 0
    try:
        from prism_service.config import PROJECTS_DIR, list_projects
        projects = list_projects() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("sqlite maintenance: project enumeration failed: %s", exc)
        return 0
    for pid in projects:
        data_dir = Path(PROJECTS_DIR) / pid
        done += checkpoint_data_dir(data_dir)
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
