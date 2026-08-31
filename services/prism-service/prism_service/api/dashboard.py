"""Dashboard API — workflow state, governance health, KPI counts, and
the activity timeline (change-over-time) that the SPA dashboard plots.

The dashboard is the *pulse* surface: it summarizes what's happening in
the brain over time (queries, indexing, workflow events, task flow)
rather than re-listing the static inventory counts that already live on
/brain. Series are cheap GROUP BY date() rollups over the same per-
project SQLite DBs used elsewhere.
"""

import logging
import sqlite3
from prism_service.services import sqlite_db
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Query

from prism_service.project_context import get_project

logger = logging.getLogger(__name__)

router = APIRouter()


def _conn(db: Path) -> "sqlite3.Connection | None":
    """One connection per DB PER REQUEST (not per query) — dashboard.py used
    to open+close a fresh connection for every _count/_rows call (~18 of
    them across 3 DBs in a single /activity response), which is tolerable
    on a fast disk but turns a slow one into death by a thousand cuts.
    cache_size/temp_store=MEMORY are read-heavy-workload tuning on top of
    sqlite_db.connect's existing WAL/busy_timeout/synchronous defaults."""
    if not db.exists():
        return None
    try:
        c = sqlite_db.connect(str(db), timeout=5.0)
        c.execute("PRAGMA cache_size = -20000")   # ~20MB of pages, negative = KB
        c.execute("PRAGMA temp_store = MEMORY")
        return c
    except Exception as exc:
        logger.warning("dashboard _conn fallback for %s: %s", db, exc)
        return None


def _count(conn: "sqlite3.Connection | None", sql: str) -> int:
    if conn is None:
        return 0
    try:
        v = conn.execute(sql).fetchone()
        return int(v[0]) if v else 0
    except Exception as exc:
        # Graceful fallback stays (dashboard must render), but never
        # swallow silently — a locked/corrupt db is an operator signal.
        logger.warning("dashboard _count fallback: %s", exc)
        return 0


def _rows(conn: "sqlite3.Connection | None", sql: str) -> list:
    if conn is None:
        return []
    try:
        return conn.execute(sql).fetchall()
    except Exception as exc:
        logger.warning("dashboard _rows fallback: %s", exc)
        return []


def _float(conn: "sqlite3.Connection | None", sql: str):
    r = _rows(conn, sql)
    return r[0][0] if r and r[0] and r[0][0] is not None else None


def _day_window(n: int) -> list:
    today = date.today()
    return [(today - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _bucket(conn: "sqlite3.Connection | None", col: str, table: str, days: list, where: str = "") -> list:
    """Count rows per calendar day, aligned to the `days` window."""
    got = {str(d): int(n)
           for d, n in _rows(conn, f"SELECT date({col}) d, COUNT(*) FROM {table} {where} GROUP BY d")
           if d}
    return [got.get(d, 0) for d in days]


def _sum_bucket(conn: "sqlite3.Connection | None", valcol: str, tscol: str, table: str, days: list) -> list:
    """Sum a value column per calendar day, aligned to the `days` window."""
    got = {str(d): int(v or 0)
           for d, v in _rows(conn, f"SELECT date({tscol}) d, SUM({valcol}) FROM {table} GROUP BY d")
           if d}
    return [got.get(d, 0) for d in days]


@router.get("/state")
def state(project: str = Query("default")) -> dict:
    ctx = get_project(project)
    s = ctx.workflow_svc.get_state()
    steps = ctx.workflow_svc.get_steps()
    health = ctx.governance.get_health_report()
    # v5.3.12 — same hardcoded-docker-path bug as v5.3.11 fixed in
    # /api/graph/summary. Use project_data_dir() so native installs
    # see real counts.
    from prism_service.config import project_data_dir
    root = project_data_dir(project)
    brain_c = _conn(root / "brain.db")
    graph_c = _conn(root / "graph.db")
    mulch_c = _conn(root / "mulch.db")
    tasks_c = _conn(root / "tasks.db")
    try:
        kpis = {
            "brain_docs": _count(brain_c, "SELECT COUNT(*) FROM docs"),
            "entities": _count(graph_c, "SELECT COUNT(*) FROM entities"),
            "relationships": _count(graph_c, "SELECT COUNT(*) FROM relationships"),
            "communities": _count(graph_c, "SELECT COUNT(DISTINCT community) FROM entities WHERE community IS NOT NULL"),
            "memories": _count(mulch_c, "SELECT COUNT(*) FROM expertise"),
            "tasks_active": _count(tasks_c, "SELECT COUNT(*) FROM tasks WHERE status IN ('pending','in_progress')"),
        }
    finally:
        for c in (brain_c, graph_c, mulch_c, tasks_c):
            if c is not None:
                c.close()
    return {
        "workflow": {"active": bool(s and s.active), "current_step": getattr(s, "current_step", None), "model": getattr(s, "model", None), "total_tokens": getattr(s, "total_tokens", 0)},
        "steps": steps,
        "health": {"flagged_conflicts": health.flagged_conflicts, "stuck_tasks": health.stuck_tasks, "stale_brain_docs": health.stale_brain_docs, "domains_near_cap": list(health.domains_near_cap), "last_governance_run": health.last_governance_run},
        "kpis": kpis,
    }


# Recent-window search health (task a91976ec). The panel counted
# n_results = 0 with NO window clause, so it showed a LIFETIME figure
# labelled as though it described a period -- 1086 of 3090 measured on
# 2026-08-31, which is every search ever recorded. Search had recovered on
# 2026-08-29, so the panel told a reader that memory search was broken when
# it was not.
#
# This computes a RECENT rate from the same rows, filtered on ts, and it
# sits BESIDE the lifetime figure rather than replacing it. Replacing it
# would hide a long-running problem instead of revealing a recovered one.
RECENT_WINDOW_DAYS = 2


def recent_zero_rate(brain_db, days=RECENT_WINDOW_DAYS, conn=None):
    """Zero-result rate over the last `days` of searches.

    Returns rate=None on an EMPTY window rather than 0.0. A zero-row window
    reporting 0.0 renders as perfect health when nothing was measured at
    all, which is the second trap this task names: a window short enough
    that silence reads as success.
    """
    from datetime import datetime, timezone

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
    total = 0
    zero = 0
    sql = ("SELECT COUNT(*), "
           "COALESCE(SUM(CASE WHEN n_results = 0 THEN 1 ELSE 0 END), 0) "
           "FROM searches WHERE ts >= ?")
    # REUSE the caller's connection when it has one. `activity` opens ONE
    # connection per db for the whole request (18 opens -> 3), and
    # test_dashboard_connection_reuse pins that: on a slow disk every extra
    # open is its own seek and fsync round trip. Opening a second one here
    # would quietly undo a measured performance fix.
    own = conn is None
    try:
        if own:
            conn = sqlite_db.connect(brain_db, timeout=5.0)
        try:
            row = conn.execute(sql, (cutoff,)).fetchone()
            if row:
                total = int(row[0] or 0)
                zero = int(row[1] or 0)
        finally:
            if own and conn is not None:
                conn.close()
    except Exception:
        return {"days": int(days), "total": 0, "zero": 0, "rate": None}
    return {"days": int(days), "total": total, "zero": zero,
            "rate": (zero / total) if total else None}


@router.get("/activity")
def activity(project: str = Query("default"), days: int = Query(14)) -> dict:
    """Change-over-time series for the dashboard pulse + interaction and
    delivery-flow detail. All values are real per-project rollups."""
    from prism_service.config import project_data_dir
    root = project_data_dir(project)
    win = _day_window(max(1, min(days, 90)))

    # ONE connection per DB for the whole request, not per query (18 opens
    # -> 3). Tolerable on a fast disk; on a slow one each open/close pair is
    # its own seek+fsync round trip, and this endpoint used to pay that 18
    # times for what only needs 3 live connections.
    brain = _conn(root / "brain.db")
    tasks = _conn(root / "tasks.db")
    scores = _conn(root / "scores.db")
    try:
        searches = _bucket(brain, "ts", "searches", win)
        indexing = _bucket(brain, "indexed_at", "docs", win)
        workflow = _bucket(tasks, "timestamp", "task_history", win)
        created = _bucket(tasks, "created_at", "tasks", win)
        completed = _bucket(tasks, "completed_at", "tasks", win, "WHERE completed_at != ''")

        lat = {str(d): round(v) for d, v in
               _rows(brain, "SELECT date(ts), AVG(latency_ms) FROM searches GROUP BY 1")
               if d and v is not None}
        recent = [{"q": q, "n_results": n, "latency_ms": ms, "ts": ts}
                  for ts, q, n, ms in _rows(brain,
                      "SELECT ts, query, n_results, latency_ms FROM searches ORDER BY ts DESC LIMIT 6")]
        q_total = _count(brain, "SELECT COUNT(*) FROM searches")
        avg_results = _float(brain, "SELECT AVG(n_results) FROM searches")
        avg_latency = _float(brain, "SELECT AVG(latency_ms) FROM searches")
        zero_results = _count(brain, "SELECT COUNT(*) FROM searches WHERE n_results = 0")
        _recent = recent_zero_rate(str(root / "brain.db"), conn=brain)

        # Token usage is recorded per work SESSION (scores.db), not per task —
        # so we report it as usage-over-time, which is what's actually tracked.
        tok_day = _sum_bucket(scores, "tokens_used", "timestamp", "session_outcomes", win)
        tok_total = _count(scores, "SELECT COALESCE(SUM(tokens_used), 0) FROM session_outcomes")
        tok_sessions = _count(scores, "SELECT COUNT(*) FROM session_outcomes WHERE tokens_used > 0")

        events = {a: int(n) for a, n in
                  _rows(tasks, "SELECT action, COUNT(*) FROM task_history GROUP BY 1 ORDER BY 2 DESC")}
        cycle = _float(tasks,
            "SELECT AVG(julianday(completed_at)-julianday(created_at)) "
            "FROM tasks WHERE completed_at != '' AND created_at != ''")
        gate_passed = _count(tasks, "SELECT COUNT(*) FROM tasks WHERE gate_state='passed'")
        gate_failed = _count(tasks, "SELECT COUNT(*) FROM tasks WHERE gate_state='failed'")
    finally:
        for c in (brain, tasks, scores):
            if c is not None:
                c.close()

    return {
        "days": win,
        "series": {"searches": searches, "indexing": indexing, "workflow": workflow},
        "queries": {
            "per_day": searches,
            "latency": [lat.get(d) for d in win],
            "recent": recent,
            "total": q_total,
            "zero": zero_results,
            # BESIDE the lifetime figure, never instead of it.
            "recent_zero": _recent["zero"],
            "recent_total": _recent["total"],
            "recent_rate": _recent["rate"],
            "recent_days": _recent["days"],
            "avg_results": round(avg_results, 2) if avg_results is not None else 0,
            "avg_latency": round(avg_latency) if avg_latency is not None else None,
        },
        "flow": {
            "created": created,
            "completed": completed,
            "events_by_action": events,
            "gate_passed": gate_passed,
            "gate_failed": gate_failed,
            "cycle_days": round(cycle, 1) if cycle is not None else None,
        },
        "tokens": {
            "per_day": tok_day,
            "total": tok_total,
            "sessions": tok_sessions,
            "avg_session": round(tok_total / tok_sessions) if tok_sessions else 0,
            "window_total": sum(tok_day),
        },
    }
