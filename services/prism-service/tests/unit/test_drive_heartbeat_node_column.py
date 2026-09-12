"""drive_heartbeat gains `node`: WHICH declared sub-node is executing right
now (task b490fabc, third pass). Additive only -- a new nullable-by-default
column, ALTERed in the same pattern as `driver` -- so conductor_service.py
(a control_plane.POLICY_FILES entry, read-only consumer of this module)
needs no change of its own.
"""
from __future__ import annotations

import sqlite3

from prism_service.services import drive_heartbeat


def test_node_column_round_trips_through_record_and_latest(tmp_path):
    db = str(tmp_path / "scores.db")
    drive_heartbeat.record_heartbeat(db, {
        "task_id": "t-1", "step": "verify_plan", "elapsed_s": 5,
        "last_tool": "x", "work_units": 1, "node": "reason-loop",
    })

    row = drive_heartbeat.latest(db, "t-1")

    assert row["node"] == "reason-loop"


def test_node_defaults_to_empty_when_not_supplied(tmp_path):
    db = str(tmp_path / "scores.db")
    drive_heartbeat.record_heartbeat(db, {
        "task_id": "t-1", "step": "verify_plan", "elapsed_s": 5,
        "last_tool": "x", "work_units": 1,
    })

    row = drive_heartbeat.latest(db, "t-1")

    assert row["node"] == ""


def test_latest_many_also_returns_node(tmp_path):
    db = str(tmp_path / "scores.db")
    drive_heartbeat.record_heartbeat(db, {
        "task_id": "t-1", "step": "verify_plan", "elapsed_s": 5,
        "last_tool": "x", "work_units": 1, "node": "text-challenge",
    })

    out = drive_heartbeat.latest_many(db, ["t-1"])

    assert out["t-1"]["node"] == "text-challenge"


def test_an_old_table_with_no_node_column_is_altered_in_place(tmp_path):
    """The same additive-migration contract the `driver` column already
    proved: a pre-existing table gains the column instead of erroring."""
    db = tmp_path / "scores.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE drive_heartbeats (
            task_id TEXT PRIMARY KEY,
            step TEXT,
            elapsed_s REAL,
            last_tool TEXT,
            work_units INTEGER,
            last_progress_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO drive_heartbeats "
        "(task_id, step, elapsed_s, last_tool, work_units, "
        " last_progress_at, recorded_at) "
        "VALUES ('t-old', 'implement_tasks', 1.0, 'x', 1, "
        " '2026-09-11T00:00:00+00:00', '2026-09-11T00:00:00+00:00')")
    conn.commit()
    conn.close()

    row = drive_heartbeat.latest(str(db), "t-old")

    assert row["node"] == ""


def test_a_node_change_advances_progress_even_with_unchanged_work_units(tmp_path):
    """A declared node moving from one route to the next is itself real
    forward motion -- it must not read as a wedged process just because
    work_units did not also move on this particular tick."""
    db = str(tmp_path / "scores.db")
    drive_heartbeat.record_heartbeat(db, {
        "task_id": "t-1", "step": "verify_plan", "elapsed_s": 5,
        "last_tool": "x", "work_units": 1, "node": "reason-loop",
    })
    first = drive_heartbeat.latest(db, "t-1")["last_progress_at"]

    drive_heartbeat.record_heartbeat(db, {
        "task_id": "t-1", "step": "verify_plan", "elapsed_s": 6,
        "last_tool": "x", "work_units": 1, "node": "text-challenge",
    })
    second = drive_heartbeat.latest(db, "t-1")

    assert second["node"] == "text-challenge"
    assert second["last_progress_at"] >= first


def test_beat_node_increments_work_units_from_the_existing_row(tmp_path):
    db = str(tmp_path / "scores.db")
    drive_heartbeat.record_heartbeat(db, {
        "task_id": "t-1", "step": "verify_plan", "elapsed_s": 5,
        "last_tool": "x", "work_units": 7,
    })

    out = drive_heartbeat.beat_node(
        db, "t-1", "verify_plan", "reason-loop", driver="prism-task-runner")

    assert out["ok"] is True
    row = drive_heartbeat.latest(db, "t-1")
    assert row["work_units"] == 8
    assert row["node"] == "reason-loop"
    assert row["driver"] == "prism-task-runner"


def test_beat_node_starts_at_one_when_no_prior_row_exists(tmp_path):
    db = str(tmp_path / "scores.db")

    drive_heartbeat.beat_node(db, "t-new", "verify_plan", "reason-loop")

    row = drive_heartbeat.latest(db, "t-new")
    assert row["work_units"] == 1
    assert row["node"] == "reason-loop"


def test_beat_node_with_empty_node_clears_it(tmp_path):
    db = str(tmp_path / "scores.db")
    drive_heartbeat.beat_node(db, "t-1", "verify_plan", "reason-loop")

    drive_heartbeat.beat_node(db, "t-1", "verify_plan", "")

    row = drive_heartbeat.latest(db, "t-1")
    assert row["node"] == ""
