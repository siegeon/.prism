"""drive_heartbeat.latest_many: the batched form of latest() (task b490fabc,
second pass). get_workflows' first fix called latest() inside a nested loop
-- one sqlite connect per behaviour entry per candidate task -- and that
measured >90s (still not returned) on a live, write-contended instance,
against ~5-50s for the same endpoint before. This is the replacement: one
connection, one query, for every task_id asked about at once.
"""
from __future__ import annotations

import sqlite3

from prism_service.services import drive_heartbeat


def test_returns_every_task_that_has_a_recorded_heartbeat(tmp_path):
    db = str(tmp_path / "scores.db")
    drive_heartbeat.record_heartbeat(db, {
        "task_id": "t-1", "step": "implement_tasks", "elapsed_s": 5,
        "last_tool": "dispatch_guard_live", "work_units": 1,
    })
    drive_heartbeat.record_heartbeat(db, {
        "task_id": "t-2", "step": "draft_story", "elapsed_s": 5,
        "last_tool": "some-tool", "work_units": 1,
    })

    out = drive_heartbeat.latest_many(db, ["t-1", "t-2", "t-3"])

    assert set(out) == {"t-1", "t-2"}, out
    assert out["t-1"]["step"] == "implement_tasks"
    assert out["t-1"]["age_s"] >= 0


def test_matches_latest_for_the_same_task(tmp_path):
    db = str(tmp_path / "scores.db")
    drive_heartbeat.record_heartbeat(db, {
        "task_id": "t-1", "step": "implement_tasks", "elapsed_s": 5,
        "last_tool": "dispatch_guard_live", "work_units": 1,
    })

    single = drive_heartbeat.latest(db, "t-1")
    batched = drive_heartbeat.latest_many(db, ["t-1"])["t-1"]

    assert single["step"] == batched["step"]
    assert single["last_progress_at"] == batched["last_progress_at"]


def test_empty_task_ids_returns_empty_without_touching_the_db(tmp_path):
    """No candidate tasks means no reason to open a connection at all --
    this must not create scores.db as a side effect."""
    db = tmp_path / "scores.db"

    out = drive_heartbeat.latest_many(str(db), [])

    assert out == {}
    assert not db.exists()


def test_duplicate_and_blank_task_ids_are_ignored(tmp_path):
    db = str(tmp_path / "scores.db")
    drive_heartbeat.record_heartbeat(db, {
        "task_id": "t-1", "step": "implement_tasks", "elapsed_s": 5,
        "last_tool": "dispatch_guard_live", "work_units": 1,
    })

    out = drive_heartbeat.latest_many(db, ["t-1", "t-1", "", None])

    assert list(out) == ["t-1"]


def test_a_row_with_a_blank_last_progress_at_is_excluded(tmp_path):
    """The column is NOT NULL, so this is the honest defensive case: a
    blank string, same falsy-check latest() itself guards against."""
    db = tmp_path / "scores.db"
    conn = sqlite3.connect(db)
    conn.execute(drive_heartbeat._SCHEMA)
    conn.execute(
        "INSERT INTO drive_heartbeats "
        "(task_id, step, elapsed_s, last_tool, work_units, "
        " last_progress_at, recorded_at, driver) "
        "VALUES ('t-1', 'implement_tasks', 1.0, 'x', 1, '', 'now', '')")
    conn.commit()
    conn.close()

    out = drive_heartbeat.latest_many(str(db), ["t-1"])

    assert out == {}


def test_uses_exactly_one_connection_for_many_task_ids(tmp_path, monkeypatch):
    """The whole point of batching: opening the connection must not scale
    with how many task_ids are asked about."""
    db = str(tmp_path / "scores.db")
    for i in range(20):
        drive_heartbeat.record_heartbeat(db, {
            "task_id": f"t-{i}", "step": "implement_tasks", "elapsed_s": 1,
            "last_tool": "x", "work_units": 1,
        })

    calls = []
    real_connect = drive_heartbeat._connect

    def _counting_connect(scores_db):
        calls.append(scores_db)
        return real_connect(scores_db)

    monkeypatch.setattr(drive_heartbeat, "_connect", _counting_connect)

    out = drive_heartbeat.latest_many(db, [f"t-{i}" for i in range(20)])

    assert len(out) == 20
    assert len(calls) == 1, calls
