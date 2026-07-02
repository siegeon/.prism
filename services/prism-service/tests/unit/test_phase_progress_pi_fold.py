"""RED — conductor phase_progress folds task-attributed pi_runs into the burn
(task fc08da8d).

The browser PI agent runs inference LOCALLY; its exchanges never land in a
Claude transcript, so a task the PI agent drove reads 0 turns / no tok/s on the
/conductor tile. phase_progress must fold the task-attributed pi_runs rows
(backend panel|pi|local, this task_id, in the work window) into the burn:
tokens_since_step gains the PI output tokens (SUM — disjoint real work from a
different agent, not a second measurement of one session) and token_turns / turns
gain the PI exchange so the tile shows the PI agent's tok/s.

FAIL today: phase_progress ignores pi_runs entirely.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PID = "pi_fold_pid"


def _conductor(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    proj_dir = tmp_path / "projects" / _PID
    proj_dir.mkdir(parents=True, exist_ok=True)
    scores_db = str(proj_dir / "scores.db")
    task_svc = TaskService(str(proj_dir / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    import sqlite3
    conn = sqlite3.connect(scores_db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_outcomes ("
            "session_id TEXT PRIMARY KEY, duration_s REAL, tokens_used INTEGER, "
            "files_read INTEGER, files_modified INTEGER, skills_invoked INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()
    return task_svc, cond


@pytest.fixture
def isolated_pi_runs(tmp_path, monkeypatch):
    import prism_service.services.pi_run_log as prl
    runs_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(prl, "_MANIFEST", runs_dir / "manifest.jsonl")
    return prl


def test_phase_progress_folds_task_attributed_pi_run(tmp_path, isolated_pi_runs):
    task_svc, cond = _conductor(tmp_path)
    prl = isolated_pi_runs

    t = task_svc.create(title="PI-driven task")
    cond.advance_task(t.id)  # onto the workflow (creates a history window)

    now = time.time()
    # A PI-panel exchange attributed to THIS task, in the work window (AFTER
    # the task's first history row), with a matching project id (scores.db
    # parent dir name).
    prl.record_run(
        backend="panel", model="qwen3:0.6b", purpose="panel-drive",
        project=_PID, task_id=t.id, output_tokens=120, tokens=120,
        ts_start=now, ts_end=now,
    )

    pp = cond.phase_progress(t.id)
    assert pp["tokens_since_step"] >= 120, (
        f"PI output tokens must fold into tokens_since_step, got {pp['tokens_since_step']}")
    assert pp["turns"] >= 1, f"the PI exchange must count as a turn, got {pp['turns']}"
    assert pp["token_turns"], "token_turns must include the PI burn point"
    assert all({"out", "dt_s", "tok_s"} <= set(turn) for turn in pp["token_turns"])


def test_pi_fold_ignores_other_tasks_and_out_of_window(tmp_path, isolated_pi_runs):
    task_svc, cond = _conductor(tmp_path)
    prl = isolated_pi_runs

    t = task_svc.create(title="PI-driven task")
    cond.advance_task(t.id)
    now = time.time()

    # Wrong task — must not fold.
    prl.record_run(backend="panel", model="m", project=_PID,
                   task_id="some-other-task", output_tokens=999, tokens=999,
                   ts_start=now - 2, ts_end=now - 1)
    # Right task but way in the future (out of window) — must not fold.
    prl.record_run(backend="panel", model="m", project=_PID,
                   task_id=t.id, output_tokens=888, tokens=888,
                   ts_start=now + 9000, ts_end=now + 9001)

    pp = cond.phase_progress(t.id)
    assert pp["tokens_since_step"] == 0, pp["tokens_since_step"]
    assert pp["turns"] == 0, pp["turns"]
    assert pp["token_turns"] == []
