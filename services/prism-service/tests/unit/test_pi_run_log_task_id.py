"""RED — pi_run_log gains task attribution (task fc08da8d).

The pi_runs ledger had no task_id, so a run the PI agent made while driving a
task could not be attributed to that task on the conductor tile or task detail.
record_run must accept an additive `task_id` (default "") that rides the
manifest row, and list_recent must gain an optional task_id filter.

FAIL today: record_run has no task_id kwarg / the row omits task_id /
list_recent ignores task_id.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def isolated_runs_dir(tmp_path, monkeypatch):
    import prism_service.services.pi_run_log as prl
    runs_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(prl, "_MANIFEST", runs_dir / "manifest.jsonl")
    return runs_dir


def test_record_run_carries_task_id(isolated_runs_dir):
    import prism_service.services.pi_run_log as prl

    run_id = prl.record_run(
        backend="panel", model="qwen3:0.6b", purpose="panel-drive",
        project="prism", task_id="fc08da8d-task", output_tokens=42, tokens=42,
    )
    assert run_id
    entry = prl.get_run(run_id)
    assert entry is not None
    assert entry["task_id"] == "fc08da8d-task"


def test_task_id_defaults_empty(isolated_runs_dir):
    import prism_service.services.pi_run_log as prl

    run_id = prl.record_run(backend="pi", model="m", project="prism")
    entry = prl.get_run(run_id)
    assert entry is not None
    assert entry["task_id"] == ""


def test_list_recent_filters_by_task_id(isolated_runs_dir):
    import prism_service.services.pi_run_log as prl

    a = prl.record_run(backend="panel", model="m", project="prism", task_id="T-1")
    _b = prl.record_run(backend="panel", model="m", project="prism", task_id="T-2")
    c = prl.record_run(backend="panel", model="m", project="prism", task_id="T-1")

    rows = prl.list_recent(limit=10, project="prism", task_id="T-1")
    assert [r["run_id"] for r in rows] == [c, a]
    assert prl.list_recent(limit=10, project="prism", task_id="never") == []
