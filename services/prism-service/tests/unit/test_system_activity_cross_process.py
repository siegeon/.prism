"""Pins system_activity.py's cross-process export/merge (task: worker-host
process split, owner 2026-09-13): once PRISM_WORKERS_PROCESS=1 moves the
standing workers into a separate OS process from the API, every
pass_()/record() call happens in THAT process -- so the API's own
snapshot() must merge in the worker host's exported feed, or the Live
page's System Activity panel would look permanently empty even while
workers are busy. snapshot()'s call signature and in-process-only
behaviour (no export file present) are UNCHANGED -- this is additive."""
from __future__ import annotations

import json
import time

from prism_service.services import system_activity


def setup_function(_fn) -> None:
    system_activity._reset_for_tests()


def test_snapshot_is_unchanged_with_no_export_file_present() -> None:
    system_activity.record("task_runner", "prism", "sweep_once",
                            started_at=time.time(), elapsed_ms=5.0)
    snap = system_activity.snapshot(project="prism")
    assert len(snap["recent"]) == 1
    assert snap["recent"][0]["kind"] == "task_runner"


def test_export_once_writes_a_readable_json_file() -> None:
    system_activity.record("ship_worker", "prism", "sweep_once",
                            started_at=time.time(), elapsed_ms=9.0)
    system_activity.export_once()
    path = system_activity._export_path()
    assert path is not None and path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "written_at" in data
    kinds = {e["kind"] for e in data["recent"]}
    assert "ship_worker" in kinds


def test_activity_exported_by_one_process_is_readable_by_another() -> None:
    # Simulate the worker host: record + export a pass.
    system_activity.record("gate_adjudicator", "prism", "sweep_once",
                            started_at=time.time(), elapsed_ms=12.0, ok=True)
    system_activity.export_once()

    # Simulate a SEPARATE API process that ran no passes of its own --
    # clear only the in-memory state, never the export file (which is
    # exactly what a second process's own empty in-memory state would
    # look like; the file is the only thing that crosses the process
    # boundary).
    with system_activity._LOCK:
        system_activity._running.clear()
        system_activity._recent.clear()

    snap = system_activity.snapshot(project="prism")
    kinds = {e["kind"] for e in snap["recent"]}
    assert "gate_adjudicator" in kinds


def test_local_and_external_recent_entries_are_merged_without_duplicates() -> None:
    system_activity.record("task_runner", "prism", "local-pass",
                            started_at=time.time(), elapsed_ms=1.0)
    exported_entry = system_activity.record(
        "ship_worker", "prism", "exported-pass",
        started_at=time.time(), elapsed_ms=1.0,
    )
    system_activity.export_once()
    # exported-pass is now BOTH in local _recent (this process just wrote
    # it) and in the export file -- merge must not double-count it.
    snap = system_activity.snapshot(project="prism")
    ids = [e["id"] for e in snap["recent"]]
    assert ids.count(exported_entry["id"]) == 1
    details = [e["detail"] for e in snap["recent"]]
    assert details.count("local-pass") == 1
    assert details.count("exported-pass") == 1


def test_a_stale_export_is_ignored() -> None:
    path = system_activity._export_path()
    path.write_text(json.dumps({
        "written_at": time.time() - 3600,
        "running": [],
        "recent": [{"id": "stale1", "kind": "ghost", "project": "prism",
                    "detail": "old", "started_at": time.time() - 3600,
                    "elapsed_ms": 1.0, "ok": True}],
        "quiet": True,
    }), encoding="utf-8")
    snap = system_activity.snapshot(project="prism")
    kinds = {e["kind"] for e in snap["recent"]}
    assert "ghost" not in kinds


def test_a_running_pass_exported_by_the_host_shows_up_as_running_here() -> None:
    with system_activity.pass_("deploy_worker", "prism", "deploy_once"):
        system_activity.export_once()
        with system_activity._LOCK:
            local_running = dict(system_activity._running)
            system_activity._running.clear()
        try:
            snap = system_activity.snapshot(project="prism")
            kinds = {e["kind"] for e in snap["running"]}
            assert "deploy_worker" in kinds
        finally:
            with system_activity._LOCK:
                system_activity._running.update(local_running)


def test_reset_for_tests_removes_the_export_file() -> None:
    system_activity.record("task_runner", "*", "x", started_at=time.time(),
                            elapsed_ms=1.0)
    system_activity.export_once()
    assert system_activity._export_path().exists()
    system_activity._reset_for_tests()
    assert not system_activity._export_path().exists()
