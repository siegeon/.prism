"""The declared flow EXECUTES; it is not read as configuration.

WHAT WAS WRONG. `.prism/behaviors/conductor/verify-plan-loop.json` declares
two steps -- `reason-loop` (persona, prompt, json_schema, rubric, caps) then
`text-challenge`. `_node_plan` opened that file and harvested SCALARS from it:
the route NAME, model, turns, budget, timeout and prompt. It never called a
declared step. Every non-agentic route was appended to a `codified` list that
only two hardcoded branches ever read by name (`premise-gather` at
task_runner.py:1445, `premise-citation-check` at :1542), so `text-challenge`
was collected and silently dropped.

The consequence is not cosmetic. `reason-loop` is where Observe (the
ContextBuilder bundle), the `json_schema` constraint and Validate (the
`plan_coverage` rubric) live -- api/workflows.py:1941-1984. Running its
PROMPT inline through claude_cli, as task_runner.py:1502 does, keeps only
Reason and drops the other three. That is why a verify_plan artifact comes
back as raw prose carrying a `<think>` wrapper and a narrated file read
instead of a schema-constrained {plan_doc, plan_diagram} object: the schema
that would have forbidden a chat transcript was declared and never applied.

WHAT THESE TESTS PIN. The declaration reaches the runner as an ORDERED LIST
OF STEPS with their bodies intact, and a generic dispatcher runs every one of
them in order. They assert the literal expected outcome -- the routes named in
the JSON, in file order -- never a function compared against its own output.
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


# ----------------------------------------------------------------------
# AC-1 -- the declaration arrives as ORDERED STEPS, not harvested scalars
# ----------------------------------------------------------------------

def test_the_plan_carries_its_declared_steps_in_file_order():
    plan = task_runner._node_plan("prism", "verify_plan")
    assert plan is not None

    routes = [s["route"] for s in plan["steps"]]
    # 7.13.365 (task a65c66e5): a codified plan-refusal-recall step now
    # runs FIRST so the planner is told what plan_gate refused.
    # 7.13.374: plan-base-colour measures the pinned suite at base before
    # the planner writes (round 2, tasks 6bc3e6c2/83dcd479).
    # 7.13.376 (v9): plan-compose builds the explicit frame before the loop.
    assert routes == ["plan-refusal-recall", "plan-base-colour", "plan-compose",
                      "reason-loop", "text-challenge"], (
        "verify-plan-loop.json declares recall, colour, compose, reason-loop, "
        "THEN text-challenge; the runner must see all five, in that order")


def test_text_challenge_is_a_step_not_a_dropped_name():
    """It used to land in `codified` and no branch ever read it."""
    plan = task_runner._node_plan("prism", "verify_plan")
    challenge = [s for s in plan["steps"] if s["route"] == "text-challenge"]
    assert challenge, "text-challenge must survive as a dispatchable step"
    assert challenge[0]["body"].get("step_id") == "verify_plan"


# ----------------------------------------------------------------------
# AC-2 -- the schema and the rubric survive the read
# ----------------------------------------------------------------------

def test_the_declared_schema_reaches_the_runner():
    """The schema is what forbids a chat transcript. It was dropped."""
    plan = task_runner._node_plan("prism", "verify_plan")
    schema = plan["json_schema"]
    assert schema["type"] == "object"
    # v9: typed slots; PRISM renders plan_doc/plan_diagram in the rubric.
    assert set(schema["properties"]) == {"goal", "acs", "files_to_change",
                                         "implementation_notes"}


def test_the_declared_rubric_reaches_the_runner():
    plan = task_runner._node_plan("prism", "verify_plan")
    assert plan["rubric"] == "plan_structured"  # v9: render + validate at the step


# ----------------------------------------------------------------------
# AC-3 -- a GENERIC dispatcher runs every declared step, in order
# ----------------------------------------------------------------------

def test_every_declared_step_is_dispatched_in_order():
    plan = task_runner._node_plan("prism", "verify_plan")
    seen: list[str] = []

    def _handler(route):
        def _run(project, body):
            seen.append(route)
            return {"ok": True, "route": route}
        return _run

    handlers = {"plan-refusal-recall": _handler("plan-refusal-recall"),
                "plan-base-colour": _handler("plan-base-colour"),
                "plan-compose": _handler("plan-compose"),
                "reason-loop": _handler("reason-loop"),
                "text-challenge": _handler("text-challenge")}
    results = task_runner._dispatch_declared_steps(
        "prism", plan, handlers=handlers)

    expected = ["plan-refusal-recall", "plan-base-colour", "plan-compose",
                "reason-loop", "text-challenge"]
    assert seen == expected
    assert [r["route"] for r in results] == expected


def test_an_undeclared_route_is_reported_never_silently_skipped():
    """A route with no handler used to vanish. It must be visible."""
    plan = task_runner._node_plan("prism", "verify_plan")
    results = task_runner._dispatch_declared_steps("prism", plan, handlers={})

    assert len(results) == 5
    assert all(r["ok"] is False for r in results)
    assert all("no handler" in r["reason"] for r in results)


def test_the_template_is_filled_before_the_step_runs():
    """A verbatim body would send the model the literal '${taskHint}'."""
    plan = task_runner._node_plan("prism", "verify_plan")
    got: dict = {}

    def _capture(project, body):
        got.update(body)
        return {"ok": True}

    task_runner._dispatch_declared_steps(
        "prism", plan, handlers={"reason-loop": _capture,
                                 "text-challenge": _capture},
        variables={"taskHint": "REAP THE WORKTREE", "taskId": "abc123"})

    assert "${taskHint}" not in got.get("prompt", "")
    assert "REAP THE WORKTREE" in got.get("prompt", "")


# ----------------------------------------------------------------------
# AC-4 -- the dispatcher is WIRED, not merely present
# ----------------------------------------------------------------------

def test_verify_plan_is_wired_to_run_as_declared_steps():
    plan = task_runner._node_plan("prism", "verify_plan")
    assert task_runner._runs_as_declared_steps("verify_plan", plan) is True


@pytest.mark.parametrize("step", ["write_failing_tests", "implement_tasks",
                                  "verify_green_state"])
def test_the_build_steps_are_never_dispatched(step):
    """Their declared prompts only DRAFT the work. Dispatching one would
    make a drive fast and green on nothing."""
    plan = task_runner._node_plan("prism", "verify_plan")
    assert task_runner._runs_as_declared_steps(step, plan) is False


def test_a_document_comes_back_as_a_result_with_its_diagram():
    class _Resp:
        reason = {"fields": {"plan_doc": "## Plan\nAC-1 oracle: run it",
                             "plan_diagram": "flowchart TD\n A-->B"}}

    out = task_runner._result_from_dispatch(
        [{"route": "reason-loop", "ok": True, "result": _Resp()}])
    assert out is not None
    assert "AC-1" in out.final_text()
    assert "```mermaid" in out.final_text()
    assert out.structured_output["plan_diagram"].startswith("flowchart TD")


def test_an_empty_document_falls_back_instead_of_advancing():
    class _Resp:
        reason = {"fields": {"plan_doc": "   ", "plan_diagram": ""}}

    assert task_runner._result_from_dispatch(
        [{"route": "reason-loop", "ok": True, "result": _Resp()}]) is None
