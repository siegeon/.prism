"""Dashboard API — workflow state, governance health, KPI counts, and
the activity timeline (change-over-time) that the SPA dashboard plots.

The dashboard is the *pulse* surface: it summarizes what's happening in
the brain over time (queries, indexing, workflow events, task flow)
rather than re-listing the static inventory counts that already live on
/brain. Series are cheap GROUP BY date() rollups over the same per-
project SQLite DBs used elsewhere.
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Query

from prism_service.project_context import get_project

router = APIRouter()


def _count(db: Path, sql: str) -> int:
    if not db.exists():
        return 0
    try:
        c = sqlite3.connect(str(db)); v = c.execute(sql).fetchone(); c.close()
        return int(v[0]) if v else 0
    except Exception:
        return 0


def _rows(db: Path, sql: str) -> list:
    if not db.exists():
        return []
    try:
        c = sqlite3.connect(str(db)); v = c.execute(sql).fetchall(); c.close()
        return v
    except Exception:
        return []


def _float(db: Path, sql: str):
    r = _rows(db, sql)
    return r[0][0] if r and r[0] and r[0][0] is not None else None


def _day_window(n: int) -> list:
    today = date.today()
    return [(today - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _bucket(db: Path, col: str, table: str, days: list, where: str = "") -> list:
    """Count rows per calendar day, aligned to the `days` window."""
    got = {str(d): int(n)
           for d, n in _rows(db, f"SELECT date({col}) d, COUNT(*) FROM {table} {where} GROUP BY d")
           if d}
    return [got.get(d, 0) for d in days]


def _sum_bucket(db: Path, valcol: str, tscol: str, table: str, days: list) -> list:
    """Sum a value column per calendar day, aligned to the `days` window."""
    got = {str(d): int(v or 0)
           for d, v in _rows(db, f"SELECT date({tscol}) d, SUM({valcol}) FROM {table} GROUP BY d")
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
    kpis = {
        "brain_docs": _count(root / "brain.db", "SELECT COUNT(*) FROM docs"),
        "entities": _count(root / "graph.db", "SELECT COUNT(*) FROM entities"),
        "relationships": _count(root / "graph.db", "SELECT COUNT(*) FROM relationships"),
        "communities": _count(root / "graph.db", "SELECT COUNT(DISTINCT community) FROM entities WHERE community IS NOT NULL"),
        "memories": _count(root / "mulch.db", "SELECT COUNT(*) FROM expertise"),
        "tasks_active": _count(root / "tasks.db", "SELECT COUNT(*) FROM tasks WHERE status IN ('pending','in_progress')"),
    }
    return {
        "workflow": {"active": bool(s and s.active), "current_step": getattr(s, "current_step", None), "model": getattr(s, "model", None), "total_tokens": getattr(s, "total_tokens", 0)},
        "steps": steps,
        "health": {"flagged_conflicts": health.flagged_conflicts, "stuck_tasks": health.stuck_tasks, "stale_brain_docs": health.stale_brain_docs, "domains_near_cap": list(health.domains_near_cap), "last_governance_run": health.last_governance_run},
        "kpis": kpis,
    }


@router.get("/activity")
def activity(project: str = Query("default"), days: int = Query(14)) -> dict:
    """Change-over-time series for the dashboard pulse + interaction and
    delivery-flow detail. All values are real per-project rollups."""
    from prism_service.config import project_data_dir
    root = project_data_dir(project)
    brain, tasks, scores = root / "brain.db", root / "tasks.db", root / "scores.db"
    win = _day_window(max(1, min(days, 90)))

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

    return {
        "days": win,
        "series": {"searches": searches, "indexing": indexing, "workflow": workflow},
        "queries": {
            "per_day": searches,
            "latency": [lat.get(d) for d in win],
            "recent": recent,
            "total": q_total,
            "zero": _count(brain, "SELECT COUNT(*) FROM searches WHERE n_results = 0"),
            "avg_results": round(avg_results, 2) if avg_results is not None else 0,
            "avg_latency": round(avg_latency) if avg_latency is not None else None,
        },
        "flow": {
            "created": created,
            "completed": completed,
            "events_by_action": events,
            "gate_passed": _count(tasks, "SELECT COUNT(*) FROM tasks WHERE gate_state='passed'"),
            "gate_failed": _count(tasks, "SELECT COUNT(*) FROM tasks WHERE gate_state='failed'"),
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
