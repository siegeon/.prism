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
        svc.record_history(t.id, "updated",
                           details="gate_reason: -> trailer not reachable",
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
    svc.record_history(t.id, "updated", details="same", actor="seat")
    first = _rows(svc, t.id)[-1]["timestamp"]
    svc.record_history(t.id, "updated", details="same", actor="seat")
    second = _rows(svc, t.id)[-1]["timestamp"]
    assert second >= first, "the collapsed row's timestamp went backwards"


def test_a_changed_detail_still_appends(tmp_path):
    """Collapsing may only ever remove a row that says nothing new.

    SUPERSEDED IN PLACE 2026-08-29: this case first wrote A, B, A and
    asserted three rows, describing it as "three distinct details". A, B, A
    is only TWO distinct details, and the repeat of A is exactly the
    oscillation the sweep storm is made of -- see
    test_an_oscillating_state_collapses_to_its_distinct_rows below. The
    invariant this case really means is that genuinely distinct details each
    keep a row, so it now uses three.
    """
    svc = _svc(tmp_path)
    t = svc.create(title="a task")
    base = len(_rows(svc, t.id))
    svc.record_history(t.id, "updated", details="refused: A", actor="seat")
    svc.record_history(t.id, "updated", details="refused: B", actor="seat")
    svc.record_history(t.id, "updated", details="refused: C", actor="seat")
    rows = _rows(svc, t.id)
    assert len(rows) == base + 3, (
        f"three distinct details must all be recorded; got {len(rows) - base}")


def test_an_oscillating_state_collapses_to_its_distinct_rows(tmp_path):
    """A DELIBERATE trade, stated so it is not mistaken for a bug.

    Two seats writing conflicting gate_reason values flip A, B, A, B on
    every 60 s sweep forever. Keeping each flip is what filled 110 MB. The
    table now keeps ONE row per distinct state, moved to the end, so the
    history says what the state is and what it was before -- not how many
    times it oscillated.
    """
    svc = _svc(tmp_path)
    t = svc.create(title="a task")
    base = len(_rows(svc, t.id))
    for _ in range(10):
        svc.record_history(t.id, "updated", details="reason: A")
        svc.record_history(t.id, "updated", details="reason: B")
    rows = _rows(svc, t.id)
    assert len(rows) - base == 2, (
        f"ten oscillations must leave the two distinct states; got {len(rows) - base}")
    assert [r["details"] for r in rows[-2:]] == ["reason: A", "reason: B"], (
        "the most recent state must be last")


def test_a_different_actor_still_appends(tmp_path):
    svc = _svc(tmp_path)
    t = svc.create(title="a task")
    base = len(_rows(svc, t.id))
    svc.record_history(t.id, "updated", details="same", actor="seat-a")
    svc.record_history(t.id, "updated", details="same", actor="seat-b")
    assert len(_rows(svc, t.id)) == base + 2, "a different actor is new information"


def test_the_real_three_row_alternating_sweep_cycle_stays_flat(tmp_path):
    """THE ACTUAL SHAPE, measured on the live board 2026-08-29.

    Each adjudicator sweep writes three rows per parked task and the
    gate_reason values ALTERNATE:

        updated      gate_reason -> "receipt is STALE"
        gate_decide  machine=refused; trailer not reachable
        updated      gate_reason -> "trailer not reachable"

    No two adjacent rows match, so collapsing only the adjacent pair left
    the growth unbounded (~26 rows/min). Twenty sweeps must leave three
    rows, not sixty.
    """
    svc = _svc(tmp_path)
    t = svc.create(title="a task parked at green_gate")
    base = len(_rows(svc, t.id))

    for _ in range(20):
        svc.record_history(t.id, "updated",
                           details='gate_reason: -> "receipt is STALE"')
        svc.record_history(t.id, "gate_decide",
                           details="machine=refused; trailer not reachable",
                           actor="conductor-adjudicator")
        svc.record_history(t.id, "updated",
                           details='gate_reason: -> "trailer not reachable"')

    rows = _rows(svc, t.id)
    updated = [r for r in rows if r["action"] == "updated"]
    decides = [r for r in rows if r["action"] == "gate_decide"]
    assert len(updated) == 2, (
        "twenty alternating sweeps must collapse to the two DISTINCT "
        f"`updated` states; got {len(updated)}")
    assert len(decides) == 20, (
        "gate_decide rows are STATE the conductor reads back, never "
        f"collapsed; got {len(decides)}")


def test_history_timestamps_still_ascend_with_id(tmp_path):
    """Consumers read this table ORDER BY id and expect ascending times, so
    a collapsed repeat moves to the END rather than being updated in place."""
    svc = _svc(tmp_path)
    t = svc.create(title="a task")
    for _ in range(6):
        svc.record_history(t.id, "updated", details="A")
        svc.record_history(t.id, "updated", details="B")
    stamps = [r["timestamp"] for r in _rows(svc, t.id)]
    assert stamps == sorted(stamps), (
        f"history timestamps went backwards against id order: {stamps}")


def test_distinct_states_beyond_the_window_each_keep_a_row(tmp_path):
    """Collapsing may only remove a row that says nothing new."""
    svc = _svc(tmp_path)
    t = svc.create(title="a task")
    base = len(_rows(svc, t.id))
    for i in range(20):
        svc.record_history(t.id, "updated", details=f"distinct change {i}")
    assert len(_rows(svc, t.id)) - base == 20, "distinct rows were dropped"


def test_state_bearing_actions_are_never_collapsed(tmp_path):
    """advance_task and gate_decide are NOT audit rows.

    The conductor reconstructs workflow and gate state by reading them back,
    so collapsing one rewrites history the state machine depends on.
    Measured: collapsing them turned
    test_gate_decide_refuses_when_state_not_pending from "failed" into
    "task is not currently on a gate step".
    """
    svc = _svc(tmp_path)
    t = svc.create(title="a task")
    for action in ("advance_task", "gate_decide"):
        base = len([r for r in _rows(svc, t.id) if r["action"] == action])
        for _ in range(10):
            svc.record_history(t.id, action, details="identical", actor="seat")
        got = len([r for r in _rows(svc, t.id) if r["action"] == action]) - base
        assert got == 10, (
            f"{action} rows are state, not audit -- all 10 must survive; got {got}")
