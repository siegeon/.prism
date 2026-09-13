"""Pins prism_service.services.system_activity's in-memory ring buffer:
record()/pass_() shape, running vs. recent, ring size, and the project
filter the /api/system/activity route relies on (owner: "until we can see
the work being done most all things you say are likely not true" -- this
is the substrate the SYSTEM ACTIVITY panel reads)."""

import time

from prism_service.services import system_activity


def setup_function(_fn) -> None:
    system_activity._reset_for_tests()


def test_record_appends_a_completed_entry_with_the_given_shape() -> None:
    entry = system_activity.record(
        "gate_adjudicator", "prism", "sweep_once",
        started_at=time.time(), elapsed_ms=12.5, ok=True,
    )
    assert entry["kind"] == "gate_adjudicator"
    assert entry["project"] == "prism"
    assert entry["detail"] == "sweep_once"
    assert entry["ok"] is True
    assert entry["elapsed_ms"] == 12.5
    assert "id" in entry and "started_at" in entry

    snap = system_activity.snapshot(project="prism")
    assert snap["running"] == []
    assert len(snap["recent"]) == 1
    assert snap["recent"][0]["kind"] == "gate_adjudicator"


def test_pass_shows_running_while_the_body_is_still_executing() -> None:
    # While the pass is still inside its `with` block, snapshot() must show
    # it as RUNNING with a live (nonzero) elapsed -- this is the whole point
    # of recording on entry rather than only on exit: a slow, in-flight pass
    # must not look like nothing is happening.
    with system_activity.pass_("task_runner", "*", "sweep_once"):
        time.sleep(0.05)
        snap = system_activity.snapshot()
        assert len(snap["running"]) == 1
        running = snap["running"][0]
        assert running["kind"] == "task_runner"
        assert running["elapsed_ms"] >= 0

    # Once the with-block exits, it must move to recent and drop from running.
    snap_after = system_activity.snapshot()
    assert snap_after["running"] == []
    assert len(snap_after["recent"]) == 1
    assert snap_after["recent"][0]["ok"] is True


def test_pass_records_ok_false_and_still_reraises_on_exception() -> None:
    raised = False
    try:
        with system_activity.pass_("ship_worker", "*", "sweep_once"):
            raise ValueError("boom")
    except ValueError:
        raised = True
    assert raised, "pass_() must never swallow the body's exception"

    snap = system_activity.snapshot()
    assert snap["running"] == []
    assert len(snap["recent"]) == 1
    assert snap["recent"][0]["ok"] is False


def test_project_filter_matches_exact_project_and_wildcard_entries() -> None:
    system_activity.record("language_alignment", "prism", "run_once_for",
                            started_at=time.time(), elapsed_ms=1.0)
    system_activity.record("language_alignment", "other-project", "run_once_for",
                            started_at=time.time(), elapsed_ms=1.0)
    system_activity.record("task_runner", "*", "sweep_once",
                            started_at=time.time(), elapsed_ms=1.0)

    snap = system_activity.snapshot(project="prism")
    kinds = {e["kind"] for e in snap["recent"]}
    projects = {e["project"] for e in snap["recent"]}
    assert "language_alignment" in kinds
    assert "task_runner" in kinds  # wildcard entries always match
    assert "other-project" not in projects


def test_recent_ring_buffer_caps_at_500_and_keeps_the_newest() -> None:
    for i in range(520):
        system_activity.record("reap_sweep", "*", f"tick-{i}",
                                started_at=time.time(), elapsed_ms=1.0)
    with system_activity._LOCK:
        assert len(system_activity._recent) == 500
    snap = system_activity.snapshot(limit=500)
    # newest first: the very last recorded tick (519) must be at index 0
    assert snap["recent"][0]["detail"] == "tick-519"
    # the oldest 20 entries (tick-0..tick-19) must have been evicted
    details = {e["detail"] for e in snap["recent"]}
    assert "tick-0" not in details


def test_snapshot_limit_bounds_the_recent_list() -> None:
    for i in range(30):
        system_activity.record("gate_adjudicator", "prism", f"tick-{i}",
                                started_at=time.time(), elapsed_ms=1.0)
    snap = system_activity.snapshot(project="prism", limit=20)
    assert len(snap["recent"]) == 20
