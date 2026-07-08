"""Sandbox job queue — an ISOLATED prove-out of PRISM's server-owned
inverted orchestration (conductor task e825e00a).

The server holds the flow and DISPENSES self-describing jobs; any client
works the pull-loop: claim -> do the work the job describes -> report.
Deliberately kept SEPARATE from the real conductor (its own sqlite file
+ table) so the experiment cannot touch real task state. New file is
justified: a distinct, throwaway concern — fold into the conductor only
once the model proves out.

A step-plan is a list of STAGES; each stage is a list of jobs. A stage
of one is a linear step; a stage of many is a FANOUT (drained
concurrently). A job with kind='gate' is released only to a worker
DISTINCT from whoever produced the prior stage (the same-actor guard).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Optional

from prism_service.data_dir import resolve_data_dir

# Visibility-timeout: a lease not reported within this window returns to
# the claimable pool on the next claim() (dead-worker recovery, no freeze).
LEASE_S = 45.0

_LOCK = threading.Lock()

_CREATE = """
CREATE TABLE IF NOT EXISTS sandbox_jobs (
  job_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  stage INTEGER NOT NULL,
  step TEXT NOT NULL,
  role TEXT DEFAULT '',
  kind TEXT DEFAULT 'agent',
  status TEXT DEFAULT 'pending',
  instructions TEXT DEFAULT '',
  expected_proof TEXT DEFAULT '',
  producer_id TEXT DEFAULT '',
  worker_id TEXT DEFAULT '',
  outcome TEXT DEFAULT '',
  lease_until REAL DEFAULT 0,
  attempts INTEGER DEFAULT 0,
  created_at REAL DEFAULT 0,
  updated_at REAL DEFAULT 0
);
"""

_PACKET_FIELDS = (
    "job_id", "task_id", "stage", "step", "role", "kind",
    "status", "instructions", "expected_proof", "producer_id",
    "worker_id", "lease_until", "attempts",
)


def _db_path() -> str:
    return str(resolve_data_dir() / "sandbox_jobs.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=5.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(_CREATE)
    return c


def _packet(row: dict) -> dict:
    return {k: row.get(k) for k in _PACKET_FIELDS}


def _counts(rows: list) -> dict:
    out = {"pending": 0, "claimable": 0, "leased": 0, "done": 0}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out


def enqueue(task_id: str, stages: list) -> dict:
    """Load a step-plan. Stage 0's jobs become claimable immediately;
    every later stage waits (pending) until its predecessor fully drains."""
    now = time.time()
    with _LOCK:
        c = _conn()
        try:
            created = []
            for si, stage in enumerate(stages):
                for job in stage:
                    jid = "j-" + uuid.uuid4().hex[:12]
                    status = "claimable" if si == 0 else "pending"
                    c.execute(
                        "INSERT INTO sandbox_jobs (job_id,task_id,stage,step,"
                        "role,kind,status,instructions,expected_proof,"
                        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (jid, task_id, si, job["step"], job.get("role", ""),
                         job.get("kind", "agent"), status,
                         job.get("instructions", ""),
                         job.get("expected_proof", ""), now, now),
                    )
                    created.append(jid)
            c.commit()
            return {"task_id": task_id, "stages": len(stages),
                    "jobs": len(created)}
        finally:
            c.close()


def claim(worker_id: str, role: Optional[str] = None,
          task_id: Optional[str] = None) -> dict:
    """Lease one claimable job for this worker. Expired leases are reaped
    back to the pool first (visibility-timeout). A gate is skipped if this
    worker produced the prior stage — the distinct-actor guard, by pull."""
    now = time.time()
    with _LOCK:
        c = _conn()
        try:
            c.execute(
                "UPDATE sandbox_jobs SET status='claimable', worker_id='', "
                "lease_until=0, updated_at=? WHERE status='leased' "
                "AND lease_until < ?", (now, now),
            )
            q = "SELECT * FROM sandbox_jobs WHERE status='claimable'"
            params: list = []
            if task_id:
                q += " AND task_id=?"
                params.append(task_id)
            if role:
                q += " AND (role=? OR role='')"
                params.append(role)
            q += " ORDER BY stage, created_at"
            chosen = None
            for r in c.execute(q, params).fetchall():
                if (r["kind"] == "gate" and r["producer_id"]
                        and r["producer_id"] == worker_id):
                    continue  # distinct-actor: can't clear your own work
                chosen = r
                break
            if chosen is None:
                c.commit()
                return {"job": None, "reason": "no claimable job you may take"}
            c.execute(
                "UPDATE sandbox_jobs SET status='leased', worker_id=?, "
                "lease_until=?, attempts=attempts+1, updated_at=? "
                "WHERE job_id=?",
                (worker_id, now + LEASE_S, now, chosen["job_id"]),
            )
            c.commit()
            row = dict(chosen)
            row.update({"status": "leased", "worker_id": worker_id,
                        "lease_until": now + LEASE_S,
                        "attempts": row["attempts"] + 1})
            return {"job": _packet(row)}
        finally:
            c.close()


def report(job_id: str, worker_id: str, outcome) -> dict:
    """Record a worker's outcome. When the job's whole STAGE has drained,
    the server promotes the next stage to claimable — stamping the prior
    producer onto any gate so the distinct-actor guard has an identity to
    refuse. This is the server advancing the flow, not the client."""
    now = time.time()
    if not isinstance(outcome, str):
        outcome = json.dumps(outcome)
    with _LOCK:
        c = _conn()
        try:
            r = c.execute("SELECT * FROM sandbox_jobs WHERE job_id=?",
                          (job_id,)).fetchone()
            if r is None:
                return {"ok": False, "error": "no such job"}
            if r["status"] != "leased":
                return {"ok": False,
                        "error": f"job is {r['status']}, not leased"}
            if r["worker_id"] != worker_id:
                return {"ok": False,
                        "error": "lease held by another worker (expired?)"}
            c.execute(
                "UPDATE sandbox_jobs SET status='done', outcome=?, "
                "updated_at=? WHERE job_id=?", (outcome, now, job_id),
            )
            task_id, stage = r["task_id"], r["stage"]
            remaining = c.execute(
                "SELECT COUNT(*) FROM sandbox_jobs WHERE task_id=? AND "
                "stage=? AND status!='done'", (task_id, stage),
            ).fetchone()[0]
            promoted = []
            if remaining == 0:
                producers = [row["worker_id"] for row in c.execute(
                    "SELECT worker_id FROM sandbox_jobs WHERE task_id=? "
                    "AND stage=?", (task_id, stage)).fetchall()]
                producer_id = producers[0] if producers else ""
                for nr in c.execute(
                    "SELECT job_id, kind FROM sandbox_jobs WHERE task_id=? "
                    "AND stage=? AND status='pending'",
                    (task_id, stage + 1)).fetchall():
                    c.execute(
                        "UPDATE sandbox_jobs SET status='claimable', "
                        "producer_id=?, updated_at=? WHERE job_id=?",
                        (producer_id if nr["kind"] == "gate" else "",
                         now, nr["job_id"]),
                    )
                    promoted.append(nr["job_id"])
            c.commit()
            return {"ok": True, "job_id": job_id, "step": r["step"],
                    "stage_complete": remaining == 0, "promoted": promoted}
        finally:
            c.close()


def state(task_id: str) -> dict:
    """Full queue snapshot for a task — what the (existing) conductor UI
    would read to show the task flowing; the queue itself stays invisible."""
    with _LOCK:
        c = _conn()
        try:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM sandbox_jobs WHERE task_id=? "
                "ORDER BY stage, created_at", (task_id,)).fetchall()]
            return {"task_id": task_id, "counts": _counts(rows),
                    "jobs": [_packet_with_outcome(r) for r in rows]}
        finally:
            c.close()


def _packet_with_outcome(row: dict) -> dict:
    p = _packet(row)
    p["outcome"] = row.get("outcome", "")
    return p
