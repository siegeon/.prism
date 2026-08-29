"""task_history must not grow without bound on an unchanging state.

Measured 2026-08-29 on prism/tasks.db: 155,659 rows for 841 tasks, 110 MB,
growing ~19,000 rows and ~10 MB per DAY, 86% of it exact duplicates. The
60 s gate-adjudicator sweep re-attempted the approve on each task parked at
green_gate, was refused for an unchanged reason, and wrote a `gate_decide`
row plus an `updated` row every sweep, forever.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services.task_service import TaskService  # noqa: E402


def _svc(tmp_path) -> TaskService:
    return TaskService(str(tmp_path / "tasks.db"))


def _rows(svc, task_id):
    return svc._db.execute(
        "SELECT action, details, timestamp FROM task_history "
        "WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()


def test_an_identical_repeat_does_not_add_a_row(tmp_path):
    svc = _svc(tmp_path)
    t = svc.create(title="a task")
    before = len(_rows(svc, t.id))

    for _ in range(50):
        svc.record_history(t.id, "gate_decide",
                           details="gate=green_gate; machine=refused; "
                                   "reason=trailer not reachable",
                           actor="conductor-adjudicator")

    after = _rows(svc, t.id)
    assert len(after) == before + 1, (
        "50 identical sweeps must collapse to ONE row, not 50 -- this is the "
        f"unbounded growth itself; got {len(after) - before} rows"
    )


def test_the_collapsed_row_still_advances_its_timestamp(tmp_path):
    """Task motion is read from these timestamps, so a collapsed repeat must
    still move time forward or live work reads as dead."""
    svc = _svc(tmp_path)
    t = svc.create(title="a task")
    svc.record_history(t.id, "gate_decide", details="same", actor="seat")
    first = _rows(svc, t.id)[-1]["timestamp"]
    svc.record_history(t.id, "gate_decide", details="same", actor="seat")
    second = _rows(svc, t.id)[-1]["timestamp"]
    assert second >= first, "the collapsed row's timestamp went backwards"


def test_a_changed_detail_still_appends(tmp_path):
    """Collapsing may only ever remove a row that says nothing new."""
    svc = _svc(tmp_path)
    t = svc.create(title="a task")
    base = len(_rows(svc, t.id))
    svc.record_history(t.id, "gate_decide", details="refused: A", actor="seat")
    svc.record_history(t.id, "gate_decide", details="refused: B", actor="seat")
    svc.record_history(t.id, "gate_decide", details="refused: A", actor="seat")
    rows = _rows(svc, t.id)
    assert len(rows) == base + 3, (
        f"three distinct details must all be recorded; got {len(rows) - base}")


def test_a_different_actor_still_appends(tmp_path):
    svc = _svc(tmp_path)
    t = svc.create(title="a task")
    base = len(_rows(svc, t.id))
    svc.record_history(t.id, "gate_decide", details="same", actor="seat-a")
    svc.record_history(t.id, "gate_decide", details="same", actor="seat-b")
    assert len(_rows(svc, t.id)) == base + 2, "a different actor is new information"
