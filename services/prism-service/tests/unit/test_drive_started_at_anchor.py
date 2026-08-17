"""Pin `_drive_started_at` (api/work.py) against a null first telemetry
row (task 9c6401dc).

Bug: GET /api/work/graph's mission-clock anchor trusted only the FIRST row
of `get_task_agent_rollup`'s ORDER BY started_at ASC, recorded_at ASC path.
SQLite sorts NULL first in ASC order, and every drive step deliberately
lands TWO agent_runs rows -- the in-process conductor row with a real
epoch, and the step agent's curl-ingested row with started_at=NULL (its
timing is server-stamped into recorded_at on ingest instead). So the NULL
row wins path[0], `_ts_epoch(None)` -> None, and the /live SPA's mission
clock never renders on any real task.

These tests call `_drive_started_at` directly (the backend anchor a
real person's browser receives verbatim as the `drive_started_at` field
on GET /api/work/graph -- see test_api_work_graph.py for the full-HTTP
pin of that field once populated) with hand-seeded agent_runs rows whose
started_at/recorded_at are fully controlled, so each AC is deterministic
regardless of wall-clock skew.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _seed_row(scores_db: str, *, run_id: str, task_id: str, step: str,
              started_at: str | None, recorded_at: str) -> None:
    """Insert one agent_runs row with an EXPLICIT recorded_at (bypassing
    the schema's `datetime('now')` default) so ordering is fully
    controlled rather than racing the wall clock within a test."""
    from prism_service.services.agent_runs_data import AGENT_RUNS_SCHEMA

    conn = sqlite3.connect(scores_db)
    try:
        conn.executescript(AGENT_RUNS_SCHEMA)
        conn.execute(
            "INSERT INTO agent_runs "
            "(run_id, task_id, agent_id, step, role, started_at, recorded_at) "
            "VALUES (?, ?, ?, ?, 'qa', ?, ?)",
            (run_id, task_id, f"{task_id}:{run_id}", step, started_at, recorded_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_two_row_step_with_null_first_row_anchors_to_the_real_epoch(tmp_path):
    # AC-1: SQLite ASC sorts NULL first, so the NULL-started_at row
    # (the curl-ingested step-agent row) sorts BEFORE the real-epoch
    # in-process conductor row. The anchor must still be the real epoch,
    # never None from trusting path[0] alone.
    from prism_service.api.work import _drive_started_at

    scores_db = str(tmp_path / "scores.db")
    _seed_row(scores_db, run_id="agent-row", task_id="t1",
              step="write_failing_tests", started_at=None,
              recorded_at="2026-08-15T00:00:05")
    _seed_row(scores_db, run_id="conductor-row", task_id="t1",
              step="write_failing_tests", started_at="1000000100.0",
              recorded_at="2026-08-15T00:00:01")

    got = _drive_started_at(scores_db, "t1")
    assert got == 1000000100.0, (
        "must anchor to the real started_at epoch, not None from the "
        f"NULL-first row; got {got!r}")


def test_all_null_started_at_falls_back_to_earliest_recorded_at(tmp_path):
    # AC-2: no row for this task carries a started_at at all -- fall back
    # to the earliest recorded_at rather than returning None outright.
    from prism_service.api.work import _drive_started_at

    scores_db = str(tmp_path / "scores.db")
    _seed_row(scores_db, run_id="row-a", task_id="t2", step="verify_plan",
              started_at=None, recorded_at="2026-08-15T00:10:00")
    _seed_row(scores_db, run_id="row-b", task_id="t2", step="draft_story",
              started_at=None, recorded_at="2026-08-15T00:05:00")

    got = _drive_started_at(scores_db, "t2")
    from datetime import datetime
    expected = datetime.fromisoformat("2026-08-15T00:05:00").timestamp()
    assert got == expected, (
        "with no started_at anywhere, must fall back to the EARLIEST "
        f"recorded_at ({expected!r}); got {got!r}")


def test_real_started_at_beats_an_earlier_recorded_at(tmp_path):
    # AC-3: the recorded_at fallback may only fire when NO row carries a
    # started_at. Here one row has a real started_at that is numerically
    # LATER than another row's recorded_at -- the real started_at must
    # still win; the earlier recorded_at must not leak in as a false
    # anchor once any real started_at exists.
    from prism_service.api.work import _drive_started_at

    scores_db = str(tmp_path / "scores.db")
    _seed_row(scores_db, run_id="row-null", task_id="t3", step="draft_story",
              started_at=None, recorded_at="2020-01-01T00:00:00")
    _seed_row(scores_db, run_id="row-real", task_id="t3",
              step="write_failing_tests", started_at="1000000500.0",
              recorded_at="2026-08-15T00:00:00")

    got = _drive_started_at(scores_db, "t3")
    assert got == 1000000500.0, (
        "a real started_at must win even though another row's recorded_at "
        f"is chronologically earlier; got {got!r}")


def test_zero_agent_runs_rows_returns_none(tmp_path):
    # AC-4a: the scores_db exists (and has rows for OTHER tasks) but this
    # task_id has none -- must return None, not raise or fabricate.
    from prism_service.api.work import _drive_started_at

    scores_db = str(tmp_path / "scores.db")
    _seed_row(scores_db, run_id="other-task-row", task_id="unrelated-task",
              step="write_failing_tests", started_at="1000000000.0",
              recorded_at="2026-08-15T00:00:00")

    got = _drive_started_at(scores_db, "t4-no-rows")
    assert got is None, f"expected None for a task with zero rows; got {got!r}"


def test_nonexistent_scores_db_path_returns_none_and_creates_no_file(tmp_path):
    # AC-4b: a scores_db path that has never been created must return
    # None AND must not have the side effect of creating the db file --
    # a mere status read must never conjure a new empty database.
    from prism_service.api.work import _drive_started_at

    scores_db = str(tmp_path / "does-not-exist.db")
    assert not Path(scores_db).exists()

    got = _drive_started_at(scores_db, "t5")

    assert got is None, f"expected None for a missing scores_db; got {got!r}"
    assert not Path(scores_db).exists(), (
        "a read against a missing scores_db must not create the file")
