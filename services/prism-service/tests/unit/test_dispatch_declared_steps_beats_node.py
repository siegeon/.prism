"""_dispatch_declared_steps beats drive_heartbeat.node for whichever
declared route is about to run, and clears it once the whole chain is done
(task b490fabc, third pass) -- the signal get_workflows reads to light the
SPECIFIC declared sub-node presently executing, not just the behaviour's
entry node.
"""
from __future__ import annotations

from prism_service.services import drive_heartbeat, task_runner


def _plan(routes):
    return {"steps": [{"route": r, "body": {}} for r in routes]}


def test_no_task_id_never_touches_the_heartbeat_table(tmp_path, monkeypatch):
    """Every existing caller (unit tests included) passes no task_id --
    behaviour must stay byte-for-byte unchanged: no scores.db side effect
    at all."""
    monkeypatch.chdir(tmp_path)
    seen = []
    handlers = {"reason-loop": lambda p, b: seen.append("reason-loop") or {"ok": True}}

    task_runner._dispatch_declared_steps(
        "prism", _plan(["reason-loop"]), handlers=handlers)

    assert seen == ["reason-loop"]
    assert not (tmp_path / "scores.db").exists()


def test_beats_node_before_each_handler_and_clears_it_after(tmp_path, monkeypatch):
    scores_db = str(tmp_path / "scores.db")
    monkeypatch.setattr(
        task_runner, "_scores_db_for", lambda project: scores_db)

    seen_nodes_at_call_time: list[str] = []

    def _handler(route):
        def _run(project, body):
            beat = drive_heartbeat.latest(scores_db, "t-1")
            seen_nodes_at_call_time.append(beat["node"] if beat else None)
            return {"ok": True}
        return _run

    handlers = {"reason-loop": _handler("reason-loop"),
                "text-challenge": _handler("text-challenge")}

    task_runner._dispatch_declared_steps(
        "prism", _plan(["reason-loop", "text-challenge"]),
        handlers=handlers, task_id="t-1", step="verify_plan")

    # Each handler, at the moment it ran, saw ITS OWN route already beaten.
    assert seen_nodes_at_call_time == ["reason-loop", "text-challenge"]

    # After the whole chain finishes, node clears -- no declared sub-step
    # is executing until the next one beats again.
    final = drive_heartbeat.latest(scores_db, "t-1")
    assert final["node"] == ""


def test_beats_carry_the_passed_step_and_the_runner_driver(tmp_path, monkeypatch):
    scores_db = str(tmp_path / "scores.db")
    monkeypatch.setattr(
        task_runner, "_scores_db_for", lambda project: scores_db)
    handlers = {"reason-loop": lambda p, b: {"ok": True}}

    task_runner._dispatch_declared_steps(
        "prism", _plan(["reason-loop"]),
        handlers=handlers, task_id="t-1", step="verify_plan")

    row = drive_heartbeat.latest(scores_db, "t-1")
    assert row["step"] == "verify_plan"
    assert row["driver"] == task_runner.RUNNER_DRIVER


def test_an_undeclared_route_still_beats_its_node_before_reporting_no_handler(tmp_path, monkeypatch):
    """A route with no handler is REPORTED, never skipped (existing rule) --
    and it still gets a beat, so the canvas can show it was reached even
    though nothing ran it."""
    scores_db = str(tmp_path / "scores.db")
    monkeypatch.setattr(
        task_runner, "_scores_db_for", lambda project: scores_db)

    out = task_runner._dispatch_declared_steps(
        "prism", _plan(["mystery-route"]),
        handlers={}, task_id="t-1", step="verify_plan")

    assert out[0]["ok"] is False
    final = drive_heartbeat.latest(scores_db, "t-1")
    assert final["node"] == ""  # cleared after the (unhandled) step finished
