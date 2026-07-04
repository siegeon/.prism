"""Pure data-access for the agent-run telemetry spine (task f4498190).

Mirrors learning_data.py: every function takes ``scores_db: str``, guards
``Path(scores_db).exists()``, opens ``sqlite3.connect`` with a Row factory,
and returns plain dicts/lists. No FastAPI / project_context coupling.

The spine is the self-heal / self-learn input: per-agent/subagent run rows
keyed (run_id, agent_id, step). Writes UPSERT on that triple so a re-POST of
the same step updates rather than duplicates.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Columns persisted on agent_runs (order == the ingest payload contract).
_COLS = (
    "run_id", "workflow_name", "task_id", "session_id", "agent_id",
    "parent_agent_id", "role", "step", "model", "started_at", "ended_at",
    "duration_ms", "tokens", "tool_uses", "ok", "gate_state",
    "verdict_summary", "evidence_ref",
)

# Filterable GET params -> agent_runs columns.
_FILTERS = ("task_id", "session_id", "workflow_name", "role", "step")


def _connect(scores_db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def upsert_agent_run(scores_db: str, row: dict) -> None:
    """Insert or update one telemetry row, idempotent on
    (run_id, agent_id, step). Booleans are coerced to 0/1 for sqlite."""
    vals = []
    for c in _COLS:
        v = row.get(c)
        if isinstance(v, bool):
            v = int(v)
        vals.append(v)
    placeholders = ", ".join("?" for _ in _COLS)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in _COLS
        if c not in ("run_id", "agent_id", "step")
    )
    sql = (
        f"INSERT INTO agent_runs ({', '.join(_COLS)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(run_id, agent_id, step) DO UPDATE SET {updates}"
    )
    conn = _connect(scores_db)
    try:
        conn.execute(sql, vals)
        conn.commit()
    finally:
        conn.close()


def get_agent_runs(scores_db: str, limit: int = 500, **filters) -> list[dict]:
    """Return agent_runs rows, newest-first, honoring task_id/session_id/
    workflow_name/role/step filters (None/empty filters are ignored)."""
    if not Path(scores_db).exists():
        return []
    where, params = [], []
    for k in _FILTERS:
        v = filters.get(k)
        if v:
            where.append(f"{k} = ?")
            params.append(v)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    conn = _connect(scores_db)
    try:
        rows = conn.execute(
            "SELECT * FROM agent_runs"
            f"{clause} ORDER BY started_at DESC, recorded_at DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["ok"] = bool(d.get("ok")) if d.get("ok") is not None else None
        out.append(d)
    return out


def get_task_agent_rollup(scores_db: str, task_id: str) -> dict:
    """Roll a task's agent_runs into total token cost + the ordered
    agent-path (role/step in chronological order). The Tier-3 self-learn
    signal: how much each task cost across its agents and the path taken."""
    if not Path(scores_db).exists():
        return {}
    conn = _connect(scores_db)
    try:
        rows = conn.execute(
            "SELECT role, step, model, tokens, duration_ms, started_at "
            "FROM agent_runs WHERE task_id = ? "
            "ORDER BY started_at ASC, recorded_at ASC",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {}
    path = [dict(r) for r in rows]
    total_tokens = sum(int(r["tokens"] or 0) for r in rows)
    total_duration = sum(int(r["duration_ms"] or 0) for r in rows)
    return {
        "task_id": task_id,
        "total_tokens": total_tokens,
        "total_duration_ms": total_duration,
        "agent_count": len(path),
        "agent_path": path,
    }


def get_agent_run_aggregates(scores_db: str) -> dict:
    """Cross-run aggregates for the /learning panel: avg duration per step,
    override rate (gate steps that ended override/blind), token cost per
    role. Returns empty lists when there is no data."""
    if not Path(scores_db).exists():
        return {"per_step": [], "per_role": [], "override_rate": 0.0,
                "total_runs": 0}
    conn = _connect(scores_db)
    try:
        per_step = [dict(r) for r in conn.execute(
            "SELECT step, COUNT(*) AS n, AVG(duration_ms) AS avg_duration_ms, "
            "       AVG(tokens) AS avg_tokens "
            "FROM agent_runs GROUP BY step ORDER BY n DESC"
        ).fetchall()]
        per_role = [dict(r) for r in conn.execute(
            "SELECT role, COUNT(*) AS n, SUM(tokens) AS total_tokens, "
            "       AVG(tokens) AS avg_tokens "
            "FROM agent_runs GROUP BY role ORDER BY total_tokens DESC"
        ).fetchall()]
        total = conn.execute(
            "SELECT COUNT(*) FROM agent_runs").fetchone()[0] or 0
        # Override rate: rows whose verdict mentions override/blind (the
        # recurring structurally-blind-verifier recovery) over all rows.
        overrides = conn.execute(
            "SELECT COUNT(*) FROM agent_runs "
            "WHERE LOWER(COALESCE(verdict_summary,'')) LIKE '%override%' "
            "   OR LOWER(COALESCE(verdict_summary,'')) LIKE '%blind%'"
        ).fetchone()[0] or 0
    finally:
        conn.close()
    return {
        "per_step": per_step,
        "per_role": per_role,
        "override_rate": (overrides / total) if total else 0.0,
        "total_runs": total,
    }
