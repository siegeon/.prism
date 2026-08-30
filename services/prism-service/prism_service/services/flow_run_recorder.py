"""Recorded node executions for the conductor canvas (task 8fbd5cf0).

The canvas used to REVERSE-MAP task_history rows into node state and to
guess how far a running step had got from a clock. Both are wrong for the
same reason: neither is a record of what a node actually did. This module
is that record.

Mirrors drive_heartbeat.py: every function takes ``scores_db: str``, opens
a plain sqlite3 connection through the shared hardening funnel, and returns
plain dicts. Non-policy -- no ``control_plane.POLICY_FILES`` entry imports
it, and it never edits ``models.workflow.WORKFLOW_STEPS`` (the terminal
``shipped`` node lives HERE, as a canvas node, not as an FSM state).

  record_node_execution  ONE row + ONE "flow.node" bus event per concluded
                         node. ``flow_version`` is always a real int.
  runs_for_task          the STORED rows. Never re-runs a check.
  progress_source        counted units only -- teeth decided/total for a
                         gate node, the drive heartbeat's own work_units
                         for an agent node. No clock, ever.
  gate_teeth             the SAME teeth /node-status already reports.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone

from prism_service.models.workflow import WORKFLOW_STEPS
from prism_service.services import sqlite_db

# The canvas node ids. The FSM's own steps, in order, plus the TERMINAL
# node the FSM does not have: a task is not finished when green_gate goes
# green, it is finished when the work landed. Drawn from the real ship
# record, never added to WORKFLOW_STEPS -- that module is read by
# conductor_service.py, a POLICY_FILES entry, so a feature task that edits
# it fails its own candidate-controls-judge tooth.
SHIPPED_NODE = "shipped"
CONDUCTOR_NODES: tuple[str, ...] = tuple(
    [str(s["id"]) for s in WORKFLOW_STEPS] + [SHIPPED_NODE])

# Which nodes are gates -- read off the same step list, so a new gate in the
# FSM is a gate here without a second list to keep in sync.
GATE_NODES: frozenset[str] = frozenset(
    str(s["id"]) for s in WORKFLOW_STEPS if s.get("type") == "gate")

# A tooth that ran and answered is a counted unit; one that never reached an
# answer is not. "unknown" is NOT progress (task 8ddbba7f: a bar that counts
# an unanswered check as done is the same lie as a clock).
_DECIDED = frozenset({"passed", "failed", "refused"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_node_runs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL DEFAULT 'conductor',
    node_id TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    flow_version INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
)
"""


def _connect(scores_db: str) -> sqlite3.Connection:
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.execute(_SCHEMA)
    return conn


def flow_version_for(workflow_id: str = "conductor") -> int:
    """A stable integer identity for the DRAWN flow this run belongs to.

    Content-derived from the node tuple, so a canvas that adds/reorders a
    node gets a different version and a stored run can never be replayed
    against a diagram it was not recorded on. Always an int, never None --
    a null version is what made the old reverse-mapped view unattributable.
    """
    digest = hashlib.sha1(
        f"{workflow_id}:{'|'.join(CONDUCTOR_NODES)}".encode("utf-8")
    ).hexdigest()[:8]
    return int(digest, 16) or 1


def record_node_execution(scores_db: str, row: dict,
                          project: str = "") -> dict:
    """Record ONE concluded node execution and announce it once.

    Writes a single flow_node_runs row (what the node said AT DECISION TIME:
    actor, outcome, reason, started_at, ended_at) and publishes exactly one
    ``flow.node`` bus event so an open canvas moves without a reload.
    Returns the stored row as a plain dict.
    """
    workflow_id = str(row.get("workflow_id") or "conductor")
    version = flow_version_for(workflow_id)
    stored = {
        "run_id": str(row.get("run_id") or uuid.uuid4().hex),
        "task_id": str(row.get("task_id") or ""),
        "workflow_id": workflow_id,
        "node_id": str(row.get("node_id") or ""),
        "actor": str(row.get("actor") or ""),
        "outcome": str(row.get("outcome") or ""),
        "reason": str(row.get("reason") or ""),
        "started_at": str(row.get("started_at") or ""),
        "ended_at": str(row.get("ended_at") or ""),
        "flow_version": version,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    conn = _connect(scores_db)
    try:
        conn.execute(
            "INSERT INTO flow_node_runs (run_id, task_id, workflow_id, "
            "node_id, actor, outcome, reason, started_at, ended_at, "
            "flow_version, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            tuple(stored[k] for k in (
                "run_id", "task_id", "workflow_id", "node_id", "actor",
                "outcome", "reason", "started_at", "ended_at",
                "flow_version", "recorded_at")),
        )
        conn.commit()
    finally:
        conn.close()

    # ONE event on the EXISTING bus -- routes/sse.py forwards it on the work
    # stream the canvas already subscribes to. Best-effort: a publish error
    # never loses the recorded row.
    try:
        from prism_service import events
        events.bus.publish({"type": "flow.node", "project": project, **stored})
    except Exception:
        pass
    return stored


def runs_for_task(scores_db: str, task_id: str,
                  workflow_id: str = "conductor") -> list[dict]:
    """The STORED node executions for one task, oldest first.

    A read path only. A concluded node reports what it said when it
    concluded, even when the live check now answers differently -- so this
    never re-runs a tooth (stop_if: "a node panel recomputes a check
    instead of reading the stored execution").
    """
    if not task_id:
        return []
    conn = _connect(scores_db)
    try:
        rows = conn.execute(
            "SELECT * FROM flow_node_runs WHERE task_id = ? AND "
            "workflow_id = ? ORDER BY seq ASC", (task_id, workflow_id),
        ).fetchall()
    finally:
        conn.close()
    return [{k: r[k] for k in r.keys()} for r in rows]


def is_finished(runs: list) -> bool:
    """True once the flow reached its terminal node -- the work landed."""
    return bool(runs) and str(runs[-1].get("node_id") or "") == SHIPPED_NODE


def is_visible(runs: list) -> bool:
    """A recorded task stays on the board. Arriving at ``shipped`` is the
    WIN state, so it must render as finished, never drop out the way
    managed_tasks drops every done row."""
    return bool(runs)


def gate_teeth(project: str, task_id: str, step: str) -> list[dict]:
    """The teeth for one gate node -- the SAME registry /node-status reports
    (api/workflows.py workflow_node_status reads plan_gate_checks.run_all).
    No second teeth registry to drift from the behaviour."""
    from prism_service.project_context import get_project
    from prism_service.services import plan_gate_checks

    if step not in GATE_NODES:
        return []
    try:
        task = get_project(project).task_svc.get(task_id)
    except Exception:
        task = None
    if task is None:
        return []
    out: list[dict] = []
    for entry in plan_gate_checks.run_all(task, project):
        out.append({"id": str(entry.get("id") or ""),
                    "status": "passed" if entry.get("ok") else "failed"})
    return out


def historical_duration_s(scores_db: str, node_id: str,
                          workflow_id: str = "conductor"):
    """The MEDIAN duration of THIS node's own past completed runs, from the
    stored started_at/ended_at pair each recorded row carries -- never a
    duration borrowed from another node, and never a shared fallback
    (stop_if: "one shared typical duration across different nodes"). None
    with no history yet, so the caller can show an honest indeterminate
    state instead of a fabricated percentage."""
    conn = _connect(scores_db)
    try:
        rows = conn.execute(
            "SELECT started_at, ended_at FROM flow_node_runs WHERE "
            "node_id = ? AND workflow_id = ? AND started_at != '' AND "
            "ended_at != '' ORDER BY seq ASC", (node_id, workflow_id),
        ).fetchall()
    finally:
        conn.close()
    durations: list[float] = []
    for r in rows:
        try:
            s = datetime.fromisoformat(str(r["started_at"]).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(r["ended_at"]).replace("Z", "+00:00"))
            d = (e - s).total_seconds()
        except Exception:
            continue
        if d > 0:
            durations.append(d)
    if not durations:
        return None
    durations.sort()
    mid = len(durations) // 2
    if len(durations) % 2:
        return durations[mid]
    return (durations[mid - 1] + durations[mid]) / 2.0


def _beat_wall_time_s(beat) -> float:
    """The heartbeat's own already-measured seconds for its last recorded
    ping. Read once, HERE, outside progress_source -- a real elapsed value
    the heartbeat subsystem timed, never a fresh clock read at the read
    path."""
    if not beat:
        return 0.0
    return float(beat.get("elapsed_s") or 0)


def progress_source(scores_db: str, task_id: str, step: str,
                    project: str = "") -> dict:
    """How far this node has got, in REAL measurement -- never a shared or
    fabricated basis.

    A gate node counts its teeth: how many answered over how many there
    are -- a real total, independent of history. An agent node with no
    history of ITS OWN yet reports the drive heartbeat's own climbing
    work_units, with no total: an honest indeterminate state, never a
    fabricated percentage (stop_if).

    SUPERSEDED 2026-08-30 -- owner: "progress bars based on historical
    durations against true wall time ... like a real factory game, where
    the factory makes tasks." The plan's open design point (no fixed
    denominator existed for an agent step) is answered: once THIS node has
    concluded before, its own past runs' median duration becomes the total,
    and the bar fills against the heartbeat's own already-measured seconds.
    Still never a duration borrowed from another node's history, and never
    a clock read at this function.
    """
    if step in GATE_NODES:
        teeth = gate_teeth(project, task_id, step)
        answered = [t for t in teeth
                    if str(t.get("status") or "") in _DECIDED]
        return {"basis": "teeth", "done": len(answered), "total": len(teeth)}
    from prism_service.services import drive_heartbeat
    beat = drive_heartbeat.latest(scores_db, task_id)
    units = 0
    done_s = 0.0
    if beat is not None and str(beat.get("step") or "") == step:
        units = int(beat.get("work_units") or 0)
        done_s = _beat_wall_time_s(beat)
    hist_s = historical_duration_s(scores_db, step)
    if hist_s is None:
        return {"basis": "work_units", "done": units, "total": None}
    return {"basis": "wall_time", "done": done_s, "total": hist_s,
            "units": units}
