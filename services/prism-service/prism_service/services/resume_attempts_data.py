"""Retry-budget bookkeeping for `resume_actuator.py` (task 7a72ebcb).

Mirrors `drive_heartbeat.py`: every function takes ``scores_db: str``,
opens a plain sqlite3 connection through the shared hardening funnel, and
returns plain dicts/ints. Non-policy -- `resume_actuator.py` is this
module's only caller.

One row per task_id, tracking how many consecutive dispatch attempts have
failed to genuinely advance the workflow_step. `record_attempt` increments
it; `reset_attempts` clears it the moment a dispatch DOES advance -- a task
that stalls once, recovers, then stalls again later starts a fresh count,
not attempt N+1 of the earlier stall.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from prism_service.services import sqlite_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resume_attempts (
    task_id TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT
)
"""


def _connect(scores_db: str) -> sqlite3.Connection:
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.execute(_SCHEMA)
    return conn


def record_attempt(scores_db: str, task_id: str) -> int:
    """Increment and return the attempt count for `task_id`."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(scores_db)
    try:
        conn.execute(
            "INSERT INTO resume_attempts (task_id, attempts, last_attempt_at) "
            "VALUES (?, 1, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "attempts = attempts + 1, last_attempt_at = excluded.last_attempt_at",
            (task_id, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT attempts FROM resume_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["attempts"]) if row is not None else 0


def attempt_count(scores_db: str, task_id: str) -> int:
    """The current attempt count for `task_id`, or 0 when none recorded."""
    conn = _connect(scores_db)
    try:
        row = conn.execute(
            "SELECT attempts FROM resume_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["attempts"]) if row is not None else 0


def last_attempt_at(scores_db: str, task_id: str) -> str:
    """When this seat last CHARGED an attempt against `task_id`, or "".

    Read by the sweep to ask whether the task advanced SINCE that moment:
    a budget spent on work that has since moved is stale (task 338f7810)."""
    conn = _connect(scores_db)
    try:
        row = conn.execute(
            "SELECT last_attempt_at FROM resume_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return str(row["last_attempt_at"] or "") if row is not None else ""


def reset_attempts(scores_db: str, task_id: str) -> None:
    """Clear the attempt budget for `task_id` -- a genuine advance resets
    it, so a later fresh stall never inherits an earlier stall's count."""
    conn = _connect(scores_db)
    try:
        conn.execute("DELETE FROM resume_attempts WHERE task_id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()
