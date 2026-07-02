"""Task 1a7bc848 — RED tests: the reflection janitor must never dispense
empty "task None" briefs.

Pins the dispense-seam gate:
  * AC-1: a null-context row (no task, empty scope) is never dispensed and
    is terminally retired (status='abandoned') so it can't re-surface.
  * AC-2: runner-owned memory-op candidates (trigger='merge', scope.op)
    are never dispensed AND stay pending for the memory-ops runner.
  * AC-3: garbage ahead in the queue does not head-of-line block a real
    actionable candidate.
  * AC-4: a non-null task_id that does not resolve in the sibling
    tasks.db counts as no-context (likely_misfire guard) — while a REAL
    task row with empty scope still dispenses.
  * AC-5: enqueue_for_session refuses new null-context session rows;
    runner-owned triggers (distill_procedural) are exempt.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class _Clock:
    def __init__(self, start: datetime) -> None:
        self._t = start

    def __call__(self) -> datetime:
        return self._t

    def advance(self, **delta) -> None:
        self._t = self._t + timedelta(**delta)


def _mk_service(tmp_path: Path, now=None):
    """JanitorService on a fresh scores.db (schema seeded via Brain)."""
    from prism_service.engines.brain_engine import Brain
    from prism_service.services.janitor_service import JanitorService

    scores_db = str(tmp_path / "scores.db")
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=scores_db,
    )
    clock = _Clock(now or datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc))
    return JanitorService(scores_db, clock=clock), clock


def _mk_tasks_db(tmp_path: Path, task_ids: list[str]) -> None:
    """Create the sibling tasks.db with real task rows so the janitor's
    task-resolvability check has something to resolve against."""
    conn = sqlite3.connect(str(tmp_path / "tasks.db"))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "  id TEXT PRIMARY KEY, title TEXT NOT NULL,"
            "  created_at TEXT NOT NULL)"
        )
        for tid in task_ids:
            conn.execute(
                "INSERT INTO tasks (id, title, created_at) VALUES (?, ?, ?)",
                (tid, f"task {tid}", "2026-07-02T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()


def _status_of(svc, cid: str) -> str:
    row = svc._db.execute(
        "SELECT status FROM consolidation_candidates WHERE id=?", (cid,)
    ).fetchone()
    return row["status"]


# ----------------------------------------------------------------------
# AC-1 — null-context rows: never dispensed, terminally retired
# ----------------------------------------------------------------------


def test_check_retires_null_context_row_terminally(tmp_path):
    svc, clock = _mk_service(tmp_path)
    cid = svc.enqueue(task_id=None, trigger="session_completed", scope={})
    clock.advance(hours=2)

    res = svc.check(session_id="S-1")
    assert res["ready"] is False, (
        "a candidate with no task and no scope context must never be "
        "dispensed as a reflection brief"
    )
    assert res["brief"] is None
    assert _status_of(svc, cid) == "abandoned", (
        "null-context rows must be terminally retired so they drain "
        "instead of cycling through abandon-backoff forever"
    )

    # And it must never re-surface — not after backoff, not ever.
    clock.advance(hours=6)
    res2 = svc.check(session_id="S-2")
    assert res2["ready"] is False


# ----------------------------------------------------------------------
# AC-2 — runner-owned candidates are not reflection briefs
# ----------------------------------------------------------------------


def test_check_skips_runner_owned_candidate_leaves_pending(tmp_path):
    svc, clock = _mk_service(tmp_path)
    cid = svc.enqueue(
        task_id=None, trigger="merge",
        scope={"op": "merge", "member_ids": ["mx-aaaaaa", "mx-bbbbbb"]},
    )
    clock.advance(hours=2)

    res = svc.check(session_id="S-1")
    assert res["ready"] is False, (
        "memory-op runner work items (trigger='merge') must never be "
        "dispensed to the caller-side reflection loop — this is exactly "
        "the 'task None' brief"
    )
    assert _status_of(svc, cid) == "pending", (
        "runner-owned candidates belong to the memory-ops runner and "
        "must stay pending for it — not retired, not dispensed"
    )


def test_check_skips_verify_staleness_trigger(tmp_path):
    svc, clock = _mk_service(tmp_path)
    cid = svc.enqueue(
        task_id=None, trigger="verify_staleness",
        scope={"memory_id": "mx-cccccc", "drifted_files": ["src/a.py"]},
    )
    clock.advance(hours=2)
    res = svc.check(session_id="S-1")
    assert res["ready"] is False
    assert _status_of(svc, cid) == "pending"


# ----------------------------------------------------------------------
# AC-3 — garbage does not head-of-line block real candidates
# ----------------------------------------------------------------------


def test_check_dispenses_actionable_past_garbage(tmp_path):
    svc, clock = _mk_service(tmp_path)
    garbage = svc.enqueue(task_id=None, trigger="session_completed", scope={})
    clock.advance(minutes=5)
    real = svc.enqueue(
        task_id="T-77", trigger="task_done",
        scope={"task_ids": ["T-77"], "file_paths": ["src/real.py"]},
    )
    clock.advance(hours=2)

    res = svc.check(session_id="S-1")
    assert res["ready"] is True
    assert res["brief"]["candidate_id"] == real
    assert res["brief"]["context"]["affected_files"] == ["src/real.py"]
    assert "task None" not in res["brief"]["question"]
    # The garbage ahead of it drained terminally in the same pass.
    assert _status_of(svc, garbage) == "abandoned"
    assert _status_of(svc, real) == "dispensed"


# ----------------------------------------------------------------------
# AC-4 — unresolvable task_id is NOT context (likely_misfire guard)
# ----------------------------------------------------------------------


def test_unresolvable_task_id_counts_as_no_context(tmp_path):
    svc, clock = _mk_service(tmp_path)
    _mk_tasks_db(tmp_path, ["T-real"])
    ghost = svc.enqueue(task_id="ghost-0000", trigger="task_done", scope={})
    clock.advance(minutes=5)
    real = svc.enqueue(task_id="T-real", trigger="task_done", scope={})
    clock.advance(hours=2)

    res = svc.check(session_id="S-1")
    assert res["ready"] is True
    assert res["brief"]["candidate_id"] == real, (
        "a resolvable task_id is real grounding even with an empty scope"
    )
    assert res["brief"]["context"]["task_id"] == "T-real"
    assert _status_of(svc, ghost) == "abandoned", (
        "a task_id that resolves to nothing must not carry an empty "
        "brief through the gate (likely_misfire: gate keys on the "
        "task_id column only)"
    )


# ----------------------------------------------------------------------
# AC-5 — enqueue-side guard for session bridge rows
# ----------------------------------------------------------------------


def test_enqueue_for_session_refuses_null_context(tmp_path):
    svc, _clock = _mk_service(tmp_path)  # seeds schema
    scores_db = str(tmp_path / "scores.db")
    from prism_service.services.consolidation_data import enqueue_for_session

    cid = enqueue_for_session(scores_db, "S-empty", scope=None)
    assert cid is None, (
        "a session with no task link and no usable scope must not "
        "enqueue a candidate — it can only ever become a 'task None' brief"
    )
    n = svc._db.execute(
        "SELECT COUNT(*) AS n FROM consolidation_candidates "
        "WHERE session_id='S-empty'"
    ).fetchone()["n"]
    assert n == 0

    # Runner-owned triggers are exempt: distill_procedural enqueues
    # cluster rows with zero signal_counts by design.
    cid2 = enqueue_for_session(
        scores_db, "distill-proc::k1",
        scope={
            "cluster": True, "member_sessions": ["a", "b"],
            "shared_skills": ["verify"], "task_id": None,
            "signal_counts": {"pushbacks": 0, "bg_signals": 0,
                              "tool_failures": 0, "memory_writes": 0},
        },
        trigger="distill_procedural",
    )
    assert cid2 is not None

    # A session with a real transcript excerpt still enqueues.
    cid3 = enqueue_for_session(
        scores_db, "S-rich",
        scope={"transcript_excerpt": "Pushbacks (2): ...",
               "signal_counts": {"pushbacks": 2}},
    )
    assert cid3 is not None
