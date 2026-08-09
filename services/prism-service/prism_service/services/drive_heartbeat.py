"""Task-attributed drive liveness heartbeat (task e3b7ebf6).

Mirrors agent_runs_data.py: every function takes ``scores_db: str``, opens
a plain sqlite3 connection through the shared hardening funnel, and returns
plain dicts. Non-policy -- conductor_service.py (a POLICY_FILES entry) only
reads this module's two public functions, it never gains write logic of
its own.

A step that runs past the 120s conductor-transition window and the 90s
session-quiet window used to render stalled/adrift with nothing wrong (the
owner's "stalled must mean the owner has something to do" complaint, task
e3b7ebf6). This module is the THIRD input: a mid-step liveness ping carrying
real progress evidence, so a driver that is genuinely working can say so.

Refuses a bare ping BY NAME (BEAT_REFUSAL) rather than silently dropping it,
and is MONOTONIC on work_units: a second beat repeating the same counter
never advances last_progress_at, so a wedged or looping process resending
its own counter cannot keep resetting its staleness clock (stop_if #4 --
"a heartbeat shape a wedged process could emit just as easily as a healthy
one").
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from prism_service.services import sqlite_db

# How long a recorded heartbeat counts as still-driving before activity_for
# falls back to the ordinary motion/quiet signals. Its own constant, never
# the 120s/90s conductor-transition windows (stop_if #2: this is a liveness
# INPUT, not a widened transition window).
HEARTBEAT_WINDOW_S = 180

# Named refusal for a ping missing a required field, so a refused caller can
# self-diagnose instead of guessing why it didn't register.
BEAT_REFUSAL = "heartbeat_missing_required_field"

_REQUIRED_FIELDS = ("task_id", "step", "elapsed_s", "last_tool", "work_units")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drive_heartbeats (
    task_id TEXT PRIMARY KEY,
    step TEXT,
    elapsed_s REAL,
    last_tool TEXT,
    work_units INTEGER,
    last_progress_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""


def _connect(scores_db: str) -> sqlite3.Connection:
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.execute(_SCHEMA)
    return conn


def record_heartbeat(scores_db: str, row: dict) -> dict:
    """Record one liveness ping for ``row["task_id"]``.

    Returns ``{"ok": True, ...}`` on acceptance, or
    ``{"ok": False, "refused": BEAT_REFUSAL, "missing": [...]}`` when a
    required field (task_id/step/elapsed_s/last_tool/work_units) is absent
    -- a bare ping is refused BY NAME, never silently dropped (AC-5).

    Monotonic: when the incoming ``work_units`` equals the PREVIOUSLY
    recorded value for this task, ``last_progress_at`` is NOT advanced --
    a wedged/looping process resending the same counter cannot pass for
    driving just by pinging again.
    """
    missing = [f for f in _REQUIRED_FIELDS if row.get(f) in (None, "")]
    if missing:
        return {"ok": False, "refused": BEAT_REFUSAL, "missing": missing}

    task_id = row["task_id"]
    work_units = row["work_units"]
    now = datetime.now(timezone.utc).isoformat()

    conn = _connect(scores_db)
    try:
        existing = conn.execute(
            "SELECT work_units, last_progress_at FROM drive_heartbeats "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if existing is not None and existing["work_units"] == work_units:
            progress_at = existing["last_progress_at"]
        else:
            progress_at = now
        conn.execute(
            "INSERT INTO drive_heartbeats "
            "(task_id, step, elapsed_s, last_tool, work_units, "
            " last_progress_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "step=excluded.step, elapsed_s=excluded.elapsed_s, "
            "last_tool=excluded.last_tool, work_units=excluded.work_units, "
            "last_progress_at=excluded.last_progress_at, "
            "recorded_at=excluded.recorded_at",
            (task_id, row["step"], row["elapsed_s"], row["last_tool"],
             work_units, progress_at, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "task_id": task_id, "last_progress_at": progress_at}


def heartbeat_age_s(scores_db: str, task_id: str):
    """Seconds since the recorded heartbeat's last_progress_at for THIS
    task_id, or None when no (accepted) heartbeat has been recorded for it
    -- strictly scoped, never global (AC-6): a beat for task B must never
    answer for task A.
    """
    if not task_id:
        return None
    conn = _connect(scores_db)
    try:
        r = conn.execute(
            "SELECT last_progress_at FROM drive_heartbeats WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if r is None or not r["last_progress_at"]:
        return None
    ts = datetime.fromisoformat(r["last_progress_at"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()
