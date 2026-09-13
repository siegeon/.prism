"""The run step DECLARES the rc it expects, and a mismatch stops the chain.

THE LIVE DEFECT (observed on task bb3d1f6a). `write-failing-tests-loop`
declares oracle-route-check -> context-enrich -> reason-loop ->
write-test-file -> run-pinned-suite -> commit-tests-only.
`run-pinned-suite` ran the task's pinned pytest suite and set
outcome="ok" for EVERY rc. So an rc=4 -- pytest could not collect,
because the drafted file never defined the pinned test id -- was reported
ok, commit-tests-only ran next, and the task's red anchor became a commit
holding a test that can never collect. red_gate needs rc==1. The integer
was in hand and nothing acted on it.

WHAT THESE TESTS PIN.

  * THE NODE FILE declares the expectation (`expected_rc: 1` in the run
    step's body), because the capability belongs in the node, not in a
    hidden Python branch.
  * THE ROUTE compares the measured rc against the declared expectation
    and REFUSES a mismatch, with `stop_chain` so the rest of the node's
    chain does not run on a bad measurement.
  * A REFUSAL STILL CARRIES THE MEASUREMENT -- rc, tail and paths -- so a
    person or an agent reading the run log sees the real integer and the
    real pytest tail. The measurement is never discarded.
  * NO DECLARED EXPECTATION means report-only, exactly as before, so every
    existing caller and test is unchanged.
  * `_dispatch_declared_steps` honours a PLAIN DICT result's `stop_chain`
    key. It read the flag with `getattr(result, "stop_chain", False)`,
    which on a dict reads an ATTRIBUTE and never a KEY, so no
    dict-returning handler -- run-pinned-suite included -- could ever stop
    the chain. The recorded row also carries the dict's own `reason`, so
    the history row is actionable instead of "ran as a declared step".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prism_service.api import workflows as wf
from prism_service.services import task_runner

_NODE = (Path(__file__).resolve().parents[4] / ".prism" / "behaviors"
         / "conductor" / "write-failing-tests-loop.json")


def _node() -> dict:
    return json.loads(_NODE.read_text(encoding="utf-8"))


def _run_step_body() -> dict:
    """The run step's body, parsed out of the JSON-encoded body string."""
    step = next(s for s in _node()["steps"]
                if "run-pinned-suite" in s.get("url", ""))
    return json.loads(step["body"])


@pytest.fixture
def suite(monkeypatch, tmp_path):
    """A scratch worktree holding one failing and one passing test, with
    every self-recorder pointed at a scratch scores.db."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "test_red.py").write_text(
        "def test_red():\n    assert False, 'not implemented yet'\n")
    (root / "test_green.py").write_text(
        "def test_green():\n    assert True\n")
    monkeypatch.setattr(wf, "_task_worktree", lambda task_id: (root, ""))
    monkeypatch.setattr(task_runner, "_scores_db_for",
                        lambda project: str(tmp_path / "scores.db"))
    return root


def _run(**kw) -> dict:
    body = wf.RunPinnedSuiteRequest(task_id="t-1", **kw)
    return wf.workflow_step_run_pinned_suite(body, project="p")


# ----------------------------------------------------------------------
# AC-1 -- the NODE FILE declares the expectation
# ----------------------------------------------------------------------

def test_the_run_step_declares_the_rc_a_red_step_needs():
    """Capability goes in the node. Without this the route has nothing to
    compare against and every rc reads as ok."""
    assert _run_step_body().get("expected_rc") == 1, (
        "the run step of write-failing-tests-loop.json does not declare "
        "expected_rc=1, so the node never tells run-pinned-suite that only "
        "rc==1 counts as red demonstrated")


def test_the_node_version_records_the_new_declaration():
    assert _node()["version"] >= 6, (
        "the declaration changed and the node version did not move")


def test_the_node_file_and_its_inner_bodies_are_valid_json():
    """The body is a JSON-encoded STRING inside a JSON file, so a careless
    edit breaks the whole behaviour and not just one step."""
    for step in _node()["steps"]:
        json.loads(step["body"])


# ----------------------------------------------------------------------
# AC-2 -- the route COMPARES, and refuses a mismatch
# ----------------------------------------------------------------------

def test_a_measured_rc_that_matches_the_expectation_is_ok(suite):
    out = _run(paths=["test_red.py"], expected_rc=1)
    assert out["outcome"] == "ok", out
    assert out["rc"] == 1, out
    assert not out.get("stop_chain"), (
        "a matching rc must let the rest of the chain run: %r" % (out,))


def test_a_collection_error_refuses_where_red_was_declared(suite):
    """THE MEASURED DEFECT. A pinned test id that was never written makes
    pytest exit 4, and commit-tests-only must not run on it."""
    out = _run(paths=["test_red.py::test_never_written"], expected_rc=1)
    assert out["outcome"] == "refused", out
    assert out["stop_chain"] is True, (
        "an rc mismatch must stop the chain, or commit-tests-only anchors "
        "red on a test that can never collect: %r" % (out,))
    assert out["rc"] == 4, out
    assert "4" in out["reason"] and "1" in out["reason"], (
        "the refusal must name the measured rc and the expected rc: "
        "%r" % (out["reason"],))
    assert "collect" in out["reason"], (
        "the refusal must say what rc=4 MEANS (pytest could not collect, a "
        "pinned test id is missing): %r" % (out["reason"],))


def test_a_passing_suite_refuses_where_red_was_declared(suite):
    """rc=0 at the red step means the assertion never failed, so there is
    nothing for red_gate to anchor on."""
    out = _run(paths=["test_green.py"], expected_rc=1)
    assert out["outcome"] == "refused", out
    assert out["stop_chain"] is True, out
    assert out["rc"] == 0, out
    assert "passed" in out["reason"], (
        "the refusal must say what rc=0 means (the tests passed where a red "
        "step needs a genuine assertion failure): %r" % (out["reason"],))


def test_a_refusal_still_carries_the_whole_measurement(suite):
    """The measurement is never discarded. A reader of the run log must see
    the real rc, the real pytest tail and the paths that were run."""
    out = _run(paths=["test_green.py"], expected_rc=1)
    assert out["outcome"] == "refused", out
    assert out["rc"] == 0, out
    assert out["paths"] == ["test_green.py"], out
    assert "1 passed" in out["tail"], (
        "the pytest tail was dropped on refusal: %r" % (out["tail"],))


# ----------------------------------------------------------------------
# AC-3 -- no declared expectation is REPORT-ONLY, exactly as before
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "paths,rc", [(["test_red.py"], 1), (["test_green.py"], 0),
                 (["test_red.py::test_never_written"], 4)])
def test_no_declared_expectation_reports_every_rc_as_ok(suite, paths, rc):
    """Every existing caller passes no expected_rc, so behaviour for them
    is unchanged: the rc is reported and the gate decides."""
    out = _run(paths=paths)
    assert out["outcome"] == "ok", out
    assert out["rc"] == rc, out
    assert not out.get("stop_chain"), out


# ----------------------------------------------------------------------
# AC-4 -- the dispatcher honours a PLAIN DICT result's stop_chain
# ----------------------------------------------------------------------

_REFUSAL = ("pytest exit code 4, expected 1: pytest could not collect "
            "(a pinned test id is missing)")


def _stopping_plan():
    ran: list[str] = []

    def _stopper(project, body):
        ran.append("first")
        return {"outcome": "refused", "stop_chain": True,
                "reason": _REFUSAL, "rc": 4}

    def _after(project, body):
        ran.append("second")
        return {"outcome": "ok"}

    plan = {"steps": [{"route": "first", "body": {}},
                      {"route": "second", "body": {}}]}
    rows = task_runner._dispatch_declared_steps(
        "prism", plan, handlers={"first": _stopper, "second": _after})
    return ran, rows


def test_a_plain_dict_result_that_says_stop_chain_stops_the_chain():
    """`getattr(a_dict, "stop_chain", False)` reads an ATTRIBUTE and is
    always False, so before this every dict-returning handler was unable
    to stop the chain no matter what it reported."""
    ran, _ = _stopping_plan()
    assert ran == ["first"], (
        "the dict result's stop_chain key was ignored and the rest of the "
        "node's chain ran anyway: %r" % (ran,))


def test_the_stopped_row_is_not_ok_and_carries_the_dicts_own_reason():
    _, rows = _stopping_plan()
    assert len(rows) == 1, rows
    assert rows[0]["ok"] is False, rows
    assert rows[0]["reason"] == _REFUSAL, (
        "the recorded row must carry the step's own refusal text, not the "
        "generic 'ran as a declared step': %r" % (rows[0]["reason"],))


def test_a_dict_result_without_the_key_does_not_stop_the_chain():
    """The early exit is keyed on the step's OWN output. An ordinary ok
    dict must thread through exactly as before."""
    ran: list[str] = []
    plan = {"steps": [{"route": "first", "body": {}},
                      {"route": "second", "body": {}}]}
    rows = task_runner._dispatch_declared_steps(
        "prism", plan,
        handlers={"first": lambda p, b: (ran.append("first"),
                                         {"outcome": "ok"})[1],
                  "second": lambda p, b: (ran.append("second"),
                                          {"outcome": "ok"})[1]})
    assert ran == ["first", "second"], ran
    assert [r["ok"] for r in rows] == [True, True], rows


# ----------------------------------------------------------------------
# AC-5 -- a stopped chain leaves NO red anchor, so the caller falls back
# ----------------------------------------------------------------------

def test_a_stopped_run_leaves_no_report_for_the_step_to_advance_on():
    """No commit-tests-only row means no red anchor, so the build chain
    must report nothing rather than advance the step on a bad rc."""
    rows = [
        {"ok": True, "route": "write-test-file",
         "result": {"outcome": "ok", "written": True, "bytes": 42,
                    "path": "tests/unit/test_x.py"}},
        {"ok": False, "route": "run-pinned-suite", "reason": _REFUSAL,
         "result": {"outcome": "refused", "stop_chain": True, "rc": 4,
                    "paths": ["tests/unit/test_x.py"], "tail": "no tests ran",
                    "reason": _REFUSAL}},
    ]
    assert task_runner._result_from_build_chain(rows) is None, (
        "the chain stopped before commit-tests-only, so there is no red "
        "anchor and the step must not advance on this chain's say-so")
