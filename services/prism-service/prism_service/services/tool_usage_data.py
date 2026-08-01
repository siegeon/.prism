"""Per-tool MCP call telemetry (task f1e7e228).

"Is this MCP tool still used?" was unanswerable from data: `call_tool`
(`mcp/server.py`) authorized, profile-checked, team-scoped and dispatched
without recording anything. This module is the write+read half of the answer.

Shape mirrors `agent_runs_data.py` function-for-function: every function takes
``scores_db: str``, opens through ``sqlite_db.connect`` with a Row factory, and
returns plain dicts/lists. No FastAPI / project_context coupling.

Rows are AGGREGATED on (project, tool, tool_profile, ok) with a call counter
rather than appended per call, so the table stays bounded no matter how chatty
a session is, and the ledger can read a real count instead of a sample.
"""

from __future__ import annotations

import sqlite3
import time

from prism_service.services import sqlite_db

_COLS = ("project", "tool", "tool_profile", "ok", "calls", "first_ts", "ts")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    project TEXT NOT NULL,
    tool TEXT NOT NULL,
    tool_profile TEXT NOT NULL,
    ok INTEGER NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    first_ts TEXT,
    ts TEXT,
    PRIMARY KEY (project, tool, tool_profile, ok)
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool);
"""


def _connect(scores_db: str) -> sqlite3.Connection:
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def record_tool_call(
    scores_db: str,
    tool: str,
    project: str,
    tool_profile: str,
    ok: bool,
) -> None:
    """Count one dispatched MCP tool call. Idempotent-by-aggregation: the
    same (project, tool, tool_profile, ok) bumps `calls` and refreshes `ts`.

    A REJECTED call is recorded too, with ok=0 — a tool a session keeps
    reaching for on the wrong profile is evidence FOR keeping it, and is the
    single most interesting row in the ledger.
    """
    now = f"{time.time():.3f}"
    conn = _connect(scores_db)
    try:
        conn.execute(
            "INSERT INTO tool_calls "
            "(project, tool, tool_profile, ok, calls, first_ts, ts) "
            "VALUES (?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(project, tool, tool_profile, ok) DO UPDATE SET "
            "calls = calls + 1, ts = excluded.ts",
            (project, tool, tool_profile, 1 if ok else 0, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_tool_calls(
    scores_db: str,
    tool: str | None = None,
    project: str | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Telemetry rows, busiest first. Returns [] when nothing was ever
    recorded — which reads as "no evidence", never as proof of death."""
    where, params = [], []
    if tool:
        where.append("tool = ?")
        params.append(tool)
    if project:
        where.append("project = ?")
        params.append(project)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    try:
        conn = _connect(scores_db)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_COLS)} FROM tool_calls{clause} "
            "ORDER BY calls DESC, ts DESC LIMIT ?",
            params,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["ok"] = bool(d.get("ok"))
        out.append(d)
    return out


def get_tool_usage_rollup(scores_db: str) -> dict[str, dict]:
    """tool -> {calls, errors, profiles, last_ts}: the shape the ledger
    generator reads to fill its "telemetry has observed a real call" column."""
    out: dict[str, dict] = {}
    for row in get_tool_calls(scores_db, limit=100_000):
        agg = out.setdefault(
            row["tool"],
            {"tool": row["tool"], "calls": 0, "errors": 0,
             "profiles": [], "last_ts": ""},
        )
        agg["calls"] += int(row["calls"] or 0)
        if not row["ok"]:
            agg["errors"] += int(row["calls"] or 0)
        if row["tool_profile"] and row["tool_profile"] not in agg["profiles"]:
            agg["profiles"].append(row["tool_profile"])
        if str(row["ts"] or "") > agg["last_ts"]:
            agg["last_ts"] = str(row["ts"] or "")
    return out
