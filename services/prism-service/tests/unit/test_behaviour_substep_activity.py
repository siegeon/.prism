"""A behaviour's sub-node lights up when it actually runs.

"I still don't see any activity" was literally true of the payload, not of
the work: _conductor_behavior_workflows built every behaviour entry with
`occupancy = {step_id: 0}` at construction time, so no sub-node could ever
be drawn live however often it fired. And nothing could have fixed that
downstream, because _record_codified_run wrote its rows with NO
started_at/ended_at at all -- there was no recency in the data to read.

Both links are pinned here: the run is stamped, and occupancy follows what
ran just now rather than a constant. A total run count cannot answer this
question -- 149 runs last week reads identically to one a second ago.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from prism_service.services.agent_runs_data import node_recent_runs


def _db(tmp_path, rows):
    db = tmp_path / "scores.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE agent_runs (step TEXT, started_at TEXT)")
    conn.executemany("INSERT INTO agent_runs (step, started_at) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def test_a_step_that_just_ran_is_recent(tmp_path):
    now = time.time()
    db = _db(tmp_path, [("premise-gather", str(now - 5)),
                        ("premise-render", str(now - 2))])

    out = node_recent_runs(str(db), ["premise-gather", "premise-render"])

    assert out == {"premise-gather": 1, "premise-render": 1}, out


def test_an_old_run_is_not_activity(tmp_path):
    """The whole point: a step with many runs long ago is NOT running."""
    now = time.time()
    db = _db(tmp_path, [("premise-gather", str(now - 86400))] * 149)

    out = node_recent_runs(str(db), ["premise-gather"])

    assert out == {"premise-gather": 0}, out


def test_a_row_with_no_timestamp_is_not_recent(tmp_path):
    """_record_codified_run wrote NULL timestamps before this change. Such
    a row must read as not-running, never as running forever."""
    db = _db(tmp_path, [("premise-gather", None)])

    assert node_recent_runs(str(db), ["premise-gather"]) == {"premise-gather": 0}


def test_an_unreadable_db_reports_no_activity_never_raises(tmp_path):
    assert node_recent_runs(str(tmp_path / "nope.db"), ["x"]) == {"x": 0}


def test_the_codified_run_is_stamped(monkeypatch):
    """Without a timestamp there is no activity signal to derive at all."""
    from prism_service.services import task_runner as tr
    from prism_service.services import agent_runs_data

    seen = {}
    monkeypatch.setattr(agent_runs_data, "upsert_agent_run",
                        lambda db, row: seen.update(row))
    monkeypatch.setattr(tr, "_scores_db_for", lambda p: ":memory:")

    tr._record_codified_run("prism", "t-1", "premise-render", "r-1", True, "ok")

    assert seen.get("started_at"), seen
    assert seen.get("ended_at"), seen
    assert float(seen["started_at"]) > 0
