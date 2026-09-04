"""An agent node's progress fill measures the step's REAL elapsed time
(task ce471e06, 2026-09-04).

`progress_source` fills an agent node with
`done=_beat_wall_time_s(beat)` over that node's own historical median —
a correct design — but EVERY writer of `elapsed_s` hardcodes zero:
`task_runner.py` and `resume_actuator.py` both beat once, before starting
a synchronous invoke, and cannot know their own elapsed at that instant.
So `done` was always 0.0 and the fill was structurally 0%, forever.

LIVE REGRESSION: the owner watched a 25-minute `verify_green_state` on
task ce471e06 render as `RUN 0s` with an empty tile, and asked "where is
my progress bar". It was drawn, wired to a real source, and fed a literal
zero by every seat.

The numerator now comes from the step's own server-stamped entry
(`advance_task` / `gate_decide`), while the denominator stays the node's
measured history — the "true wall time against historical duration"
design, with no fabricated percentage: with no transition row to measure
from it returns 0.0 and the caller falls back to the indeterminate basis.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

STEP = "verify_green_state"


@pytest.fixture()
def task_in_step(tmp_path):
    from prism_service.project_context import get_project

    project = "progress-" + uuid.uuid4().hex[:8]
    ctx = get_project(project)
    task = ctx.task_svc.create(title="progress task")
    ctx.task_svc.update(task.id, status="in_progress", workflow_step=STEP)
    # A REAL path under tmp_path, never a bare "unused.db": that literal
    # is created in the CWD by _connect(), and a stray sqlite file in a
    # task's own worktree trips the green_gate's uncommitted-changes
    # tooth. Observed live on ce471e06 (2026-09-04): the gate refused a
    # shipped task over '?? services/prism-service/unused.db'.
    return ctx, task.id, project, str(tmp_path / "scores.db")


def _entered_step(ctx, task_id: str, seconds_ago: float) -> None:
    """Record the conductor transition that put the task in this step, then
    backdate it `seconds_ago`.

    `record_history` stamps `now` and takes no timestamp, so the row is
    written through the real service (keeping its shape honest) and its
    timestamp is moved directly in the store afterwards — the same table
    `_step_elapsed_s` reads."""
    ctx.task_svc.record_history(
        task_id, action="advance_task",
        details=f"from=implement_tasks; to={STEP}", actor="conductor")
    when = (datetime.now(timezone.utc)
            - timedelta(seconds=seconds_ago)).isoformat()
    import sqlite3

    conn = sqlite3.connect(str(ctx._data_dir / "tasks.db"))
    try:
        conn.execute(
            "UPDATE task_history SET timestamp = ? "
            "WHERE task_id = ? AND action = 'advance_task'",
            (when, task_id))
        conn.commit()
    finally:
        conn.close()


def test_step_elapsed_measures_from_the_transition_that_entered_the_step(
        task_in_step):
    from prism_service.services import flow_run_recorder as fr

    ctx, task_id, project, db = task_in_step
    _entered_step(ctx, task_id, 600.0)

    got = fr._step_elapsed_s(project, task_id)

    assert 570.0 <= got <= 660.0, (
        f"a step entered 10 minutes ago must measure ~600s, got {got}")


def test_no_transition_row_measures_nothing_rather_than_inventing_it(
        task_in_step):
    """Honesty floor: with nothing server-stamped to measure from, the
    answer is 0.0 — the caller then shows an indeterminate state instead
    of a fabricated percentage."""
    from prism_service.services import flow_run_recorder as fr

    _ctx, task_id, project, db = task_in_step

    assert fr._step_elapsed_s(project, task_id) == 0.0


def test_progress_source_fills_despite_a_zero_elapsed_heartbeat(
        task_in_step, monkeypatch):
    """The live shape end to end: a seat's beat carries elapsed_s=0 (as all
    of them do) and the node still reports real progress."""
    from prism_service.services import drive_heartbeat
    from prism_service.services import flow_run_recorder as fr

    ctx, task_id, project, db = task_in_step
    _entered_step(ctx, task_id, 300.0)

    monkeypatch.setattr(
        drive_heartbeat, "latest",
        lambda db, tid: {"step": STEP, "elapsed_s": 0, "work_units": 2,
                         "last_tool": "claude_cli.invoke"})
    monkeypatch.setattr(fr, "historical_duration_s", lambda db, step: 900.0)

    got = fr.progress_source(db, task_id, STEP, project=project)

    assert got["basis"] == "wall_time"
    assert got["total"] == 900.0
    assert got["done"] > 0, (
        "the bar must fill on real step elapsed — a hardcoded elapsed_s=0 "
        "from the seat is what pinned it at 0% forever")
    assert 270.0 <= got["done"] <= 360.0


def test_a_real_measured_beat_still_wins(task_in_step, monkeypatch):
    """A seat that DOES measure its own elapsed keeps that value — this
    change supplies a missing numerator, it never overrides a real one."""
    from prism_service.services import drive_heartbeat
    from prism_service.services import flow_run_recorder as fr

    ctx, task_id, project, db = task_in_step
    _entered_step(ctx, task_id, 300.0)

    monkeypatch.setattr(
        drive_heartbeat, "latest",
        lambda db, tid: {"step": STEP, "elapsed_s": 42.0, "work_units": 2,
                         "last_tool": "claude_cli.invoke"})
    monkeypatch.setattr(fr, "historical_duration_s", lambda db, step: 900.0)

    got = fr.progress_source(db, task_id, STEP, project=project)

    assert got["done"] == 42.0


def test_no_history_for_this_node_stays_indeterminate(
        task_in_step, monkeypatch):
    """With no historical median there is no honest denominator, so the
    basis stays work_units and no percentage is drawn."""
    from prism_service.services import drive_heartbeat
    from prism_service.services import flow_run_recorder as fr

    ctx, task_id, project, db = task_in_step
    _entered_step(ctx, task_id, 300.0)

    monkeypatch.setattr(
        drive_heartbeat, "latest",
        lambda db, tid: {"step": STEP, "elapsed_s": 0, "work_units": 3,
                         "last_tool": "claude_cli.invoke"})
    monkeypatch.setattr(fr, "historical_duration_s", lambda db, step: None)

    got = fr.progress_source(db, task_id, STEP, project=project)

    assert got["basis"] == "work_units"
    assert got["total"] is None
    assert got["done"] == 3
