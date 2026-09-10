"""write_failing_tests WRITES, RUNS and COMMITS -- it does not draft.

WHAT WAS WRONG. `.prism/behaviors/conductor/write-failing-tests-loop.json`
declared exactly one node: a `reason-loop` whose prompt opens "Draft a
failing test (do NOT write it to disk, this is a DRAFT only)". The
Workflows canvas drew that single node ("8 runs, no token cost"), so the
step had no failing-test GENERATOR at all -- nothing wrote a file, nothing
ran pytest, nothing left a tests-only commit for the red seat to anchor on.

Because a draft cannot satisfy the step, `_run_step` fell through to the
general inline `claude_cli.invoke`. On task d5808cd1 that envelope measured
133,780 tokens against the engine's 131,072 window, so the call 400'd with
ContextWindowExceededError before inference. 27 dispatches recorded no
model run, and the resume actuator parked the task "for a person".

WHAT THESE TESTS PIN. The three clauses of task ab9166d5's oracle that are
about BEHAVIOUR, not declaration text -- the misfire note is explicit that
"a test that reads the JSON file only" does not count:

  * the chain THREADS -- the draft's test_code reaches the write node,
    rather than the literal string "${testCode}";
  * the chain REPORTS -- a run that wrote, ran and committed comes back as
    a step result carrying the real pytest rc, so the caller does NOT fall
    back to the inline call that 400s;
  * the chain REFUSES -- a run that left no tests-only commit returns None
    so the caller DOES fall back, because a declared flow that misfires
    must never advance a step on nothing.

The membership guard is pinned against the WORK the declaration can do,
never against the step name, so removing a route from the JSON drops the
step back to the general agent instead of advancing it on a draft.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_service.services import task_runner

_REPO_BEHAVIORS = (Path(__file__).resolve().parents[4]
                   / ".prism" / "behaviors" / "conductor")


@pytest.fixture(autouse=True)
def _hermetic_behavior_dir(monkeypatch):
    monkeypatch.setattr(
        task_runner, "_behavior_dir", lambda project: _REPO_BEHAVIORS)


def _plan():
    return task_runner._node_plan("prism", "write_failing_tests")


# ----------------------------------------------------------------------
# AC-1 -- the node declares the work, and the guard reads the WORK
# ----------------------------------------------------------------------

def test_the_declaration_carries_write_run_and_commit():
    """Clause 1 of the oracle. The draft node alone is what was wrong."""
    plan = _plan()
    assert plan is not None, "write-failing-tests-loop.json must be readable"
    routes = [s.get("route") for s in plan["steps"]]
    for needed in ("write-test-file", "run-pinned-suite",
                   "commit-tests-only"):
        assert needed in routes, (
            f"the step has no failing-test generator without {needed!r}; "
            f"declared routes are {routes}")


def test_every_declared_route_has_a_handler():
    """Clause 2/3: each route must ANSWER, or the dispatcher reports
    'no handler' for it and the chain silently does nothing."""
    plan = _plan()
    assert plan is not None, "write-failing-tests-loop.json must be readable"
    table = task_runner._step_handlers()
    missing = [s["route"] for s in plan["steps"]
               if s.get("route") not in table]
    assert missing == [], f"declared routes with no handler: {missing}"


def test_the_guard_admits_the_step_only_when_it_can_do_the_work():
    plan = _plan()
    assert task_runner._runs_as_declared_steps(
        "write_failing_tests", plan) is True


def test_a_draft_only_declaration_is_refused():
    """THE MISFIRE THE TICKET NAMES. Admitting the step while the file
    still declares the draft alone would advance it with no tests-only
    commit and no red anchor -- fast and green on nothing."""
    draft_only = {"steps": [{"route": "reason-loop", "body": {}}],
                  "json_schema": {"type": "object"}}
    assert task_runner._runs_as_declared_steps(
        "write_failing_tests", draft_only) is False


@pytest.mark.parametrize("route", ["write-test-file", "run-pinned-suite",
                                   "commit-tests-only"])
def test_dropping_any_single_route_refuses_the_step(route):
    """The guard requires all three by name. Any one missing and the step
    falls back to the general agent rather than advancing on a draft."""
    plan = _plan()
    assert plan is not None, "write-failing-tests-loop.json must be readable"
    crippled = {"steps": [s for s in plan["steps"]
                          if s.get("route") != route],
                "json_schema": plan.get("json_schema")}
    assert task_runner._runs_as_declared_steps(
        "write_failing_tests", crippled) is False


@pytest.mark.parametrize("step", ["implement_tasks", "verify_green_state"])
def test_the_remaining_draft_steps_are_still_never_dispatched(step):
    """Unchanged by this slice. Their declarations still only DRAFT
    ("do NOT write any code", "OBSERVE-only check")."""
    assert task_runner._runs_as_declared_steps(step, _plan()) is False


# ----------------------------------------------------------------------
# AC-2 -- the chain THREADS: an earlier step's output fills a later one
# ----------------------------------------------------------------------

def test_the_draft_reaches_the_write_node():
    """Without threading, _subst leaves ${testCode} verbatim and the write
    node is handed the literal placeholder as a file body."""
    assert hasattr(task_runner, "_exported_variables"), (
        "no _exported_variables: a later declared step cannot read an "
        "earlier one's output yet")

    class _Draft:
        reason = {"fields": {"test_file_path": "tests/unit/test_x.py",
                             "test_code": "def test_x():\n    assert False"}}

    seen: dict = {}

    def _draft(project, body):
        return _Draft()

    def _capture(project, body):
        seen.update(body)
        return {"outcome": "ok", "written": True}

    plan = _plan()
    handlers = {s["route"]: _capture for s in plan["steps"]}
    handlers["reason-loop"] = _draft

    task_runner._dispatch_declared_steps(
        "prism", plan, handlers=handlers,
        variables={"taskHint": "h", "taskId": "abc123"})

    assert seen.get("test_code") == "def test_x():\n    assert False", (
        f"the write node got {seen.get('test_code')!r}, not the drafted "
        f"test body")
    assert "${" not in str(seen.get("test_file_path", "")), (
        f"unfilled placeholder reached the node: {seen.get('test_file_path')!r}")


# ----------------------------------------------------------------------
# AC-3 -- the chain REPORTS, so the caller does not fall back and 400
# ----------------------------------------------------------------------

def _rows(*, committed=True, rc=1):
    return [
        {"ok": True, "route": "write-test-file",
         "result": {"outcome": "ok", "written": True, "bytes": 42,
                    "path": "tests/unit/test_x.py"}},
        {"ok": True, "route": "run-pinned-suite",
         "result": {"outcome": "ok", "rc": rc,
                    "paths": ["tests/unit/test_x.py"],
                    "tail": "1 failed, 3 passed in 0.42s"}},
        {"ok": True, "route": "commit-tests-only",
         "result": {"outcome": "ok" if committed else "refused",
                    "committed": committed, "sha": "deadbeefcafe",
                    "files": ["tests/unit/test_x.py"]}},
    ]


def test_a_completed_chain_comes_back_as_a_result():
    """If this returns None the caller sets dispatched=None and runs the
    inline claude call -- the 133,780-token envelope that 400s."""
    out = task_runner._result_from_dispatch(_rows())
    assert out is not None, (
        "the write/run/commit chain produced a red anchor and still "
        "reported nothing, so the caller falls back to the inline call")
    assert out.exit_code == 0


def test_the_report_carries_the_real_pytest_output():
    """conductor_service expects real pytest text in this proof; the red
    receipt is minted off this report."""
    out = task_runner._result_from_dispatch(_rows())
    assert out is not None, "the chain reported nothing to carry the rc"
    text = out.final_text()
    assert "1 failed, 3 passed in 0.42s" in text
    assert "deadbeefcafe"[:12] in text
    assert "tests/unit/test_x.py" in text


def test_the_reported_rc_is_the_measured_one():
    """The node REPORTS the rc and the gate judges it. oracle_spec's red
    check wants rc==1 and refuses 0, 2 and 4 by name."""
    for rc in (0, 1, 2, 4):
        out = task_runner._result_from_dispatch(_rows(rc=rc))
        assert out is not None, f"the chain reported nothing for rc {rc}"
        text = out.final_text()
        assert f"exit code {rc}" in text, (
            f"rc {rc} must be reported verbatim, never interpreted")


def test_no_commit_falls_back_instead_of_advancing():
    """No tests-only commit means no red anchor, so the step must NOT
    advance on the chain's say-so."""
    assert task_runner._result_from_dispatch(
        _rows(committed=False)) is None


def test_a_draft_never_substitutes_for_the_step():
    """THE REGRESSION THIS SLICE COULD HAVE CAUSED. Giving the step a
    narrow prompt makes an inline fallback possible, and that prompt says
    "do NOT write it to disk" -- so it returns prose with exit 0 and the
    step would advance with no tests-only commit. Before this slice the
    fallback ran the full brief and 400'd LOUDLY, which is worse for cost
    but better for honesty. The step must never advance on a draft."""
    assert hasattr(task_runner, "_DRAFT_ONLY_WITHOUT_CHAIN"), (
        "nothing stops the draft prompt standing in for the step")
    assert "write_failing_tests" in task_runner._DRAFT_ONLY_WITHOUT_CHAIN


def test_the_step_keeps_its_own_wall_clock():
    """The retired guard's REAL invariant: a build step is not governed by
    the template budget (haiku / 4 turns / $0.50 / 120 s). _invoke_budget
    keeps _step_timeout_s, which gives write_failing_tests its 2.0
    multiplier, so joining _PLANNED_STEPS must not shrink the clock."""
    plan = _plan()
    budget = task_runner._invoke_budget(
        "write_failing_tests", plan, narrow=True)
    assert budget["timeout_s"] == task_runner._step_timeout_s(
        "write_failing_tests"), (
        "the declared 120 s template must not replace the step's own clock")
    assert budget["timeout_s"] >= 1800


def test_the_document_chain_is_unchanged():
    """verify_plan's reason-loop -> plan_doc shape still works."""
    class _Resp:
        reason = {"fields": {"plan_doc": "## Plan\nAC-1 oracle: run it",
                             "plan_diagram": "flowchart TD\n A-->B"}}

    out = task_runner._result_from_dispatch(
        [{"route": "reason-loop", "ok": True, "result": _Resp()}])
    assert out is not None and "AC-1" in out.final_text()
