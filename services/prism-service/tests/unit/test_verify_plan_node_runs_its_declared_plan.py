"""verify_plan runs its DECLARED narrow middle (task a9f2bec7).

MEASURED 2026-09-10 on the live daemon, same model, same CPU, same box:

    draft_story  narrow declared plan, zero tools ..  23.9s  exit 0, rubric-green
    verify_plan  full brief + BUILD_TOOLS .......... 900.1s  exit -9, no report

The discriminator is what we send the model, not the model and not the clock.
`.prism/behaviors/conductor/verify-plan-loop.json` has declared a complete
narrow middle since version 3 -- route reason-loop, haiku, 4 turns, $0.50, a
1295-character prompt, schema [plan_doc, plan_diagram], rubric plan_coverage --
and nothing read it, because `_node_plan` returns None for any step outside
`_PLANNED_STEPS` and that set named only two steps.

TWO HALVES, AND THE SECOND IS THE ONE THAT BITES. Adding verify_plan to the
allowlist alone buys a FASTER FAILURE, not a fix: `_invoke_budget` adopts the
declared turn and spend caps only when `narrow=True`, and narrow is true only
when `_declared_agentic_prompt` returns something. So the prompt branch is the
fix; the allowlist entry is bookkeeping.

And the proof must be SPLIT. `_route_proof` funnelled the whole report into
plan_doc for every `_PLAN_STEPS` member, while `arc_governance.score_plan_*`
reads the plan_diagram FIELD (arc_governance.py:279-285: missing, or not
parsing as mermaid, is a refusal). A narrow verify_plan that returns one
markdown blob would populate plan_doc and leave plan_diagram empty, trading a
900s timeout for a gate refusal -- a worse trade, because a refusal reads as
the drive's fault.

These tests assert the LITERAL expected outcome, never a function compared
against its own output.
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


class _Task:
    title = "A hand landed task still reaches the reap"
    description = "The reap fires only from ship_worker."


# ----------------------------------------------------------------------
# AC-1 -- the declared plan is READ for verify_plan
# ----------------------------------------------------------------------

def test_verify_plan_is_a_planned_step():
    assert "verify_plan" in task_runner._PLANNED_STEPS
    plan = task_runner._node_plan("prism", "verify_plan")
    assert plan is not None, "verify-plan-loop.json declares a real plan"
    assert plan["model"] == "haiku"
    assert plan["max_turns"] == 4
    assert plan["max_budget_usd"] == 0.5


def test_the_draft_only_build_steps_stay_out_of_the_allowlist():
    """REGRESSION GUARD, not an oversight. implement-tasks-loop and
    verify-green-state-loop declare prompts that open "Draft an
    implementation approach (do NOT write any code)" and "OBSERVE-only
    check". Those steps must CHANGE the code and RUN the suite. Wiring one
    would make drives fast and green on nothing, which is strictly worse
    than a timeout.

    SUPERSEDED IN PART 2026-09-10 by task ab9166d5, which removed
    write_failing_tests from this list. The invariant was never the step
    NAME -- it was that a step whose declaration can only DRAFT must not be
    dispatched. write-failing-tests-loop.json now also declares
    write-test-file, run-pinned-suite and commit-tests-only, so the chain
    does the work the draft cannot, and the membership test moved to
    _runs_as_declared_steps, which reads the declared ROUTES. The live
    contract is pinned in test_write_failing_tests_runs_as_declared_nodes.py
    (a draft-only declaration is still refused, and a draft never
    substitutes for the step when the chain does not run).
    """
    for step in ("implement_tasks", "verify_green_state"):
        assert step not in task_runner._PLANNED_STEPS, (
            f"{step} does heavy work; its declared prompt only DRAFTS it")


# ----------------------------------------------------------------------
# AC-2 -- the narrow prompt is what actually reaches the model
# ----------------------------------------------------------------------

def test_verify_plan_sends_the_declared_narrow_prompt():
    plan = task_runner._node_plan("prism", "verify_plan")
    prompt = task_runner._declared_agentic_prompt(
        "verify_plan", _Task(), [], plan=plan)

    assert prompt, "verify_plan must have a narrow prompt, not the step brief"
    assert "implementation plan" in prompt
    assert "plan_diagram" in prompt, "the diagram is half the deliverable"
    assert "AC-" in prompt, "the plan must reference the story's AC ids"
    assert _Task.title in prompt, "the task material is substituted in"
    assert "${taskHint}" not in prompt, "the placeholder must be filled"


def test_verify_plan_falls_back_to_the_full_brief_with_no_declaration():
    """No plan in hand means no narrow prompt. The full step brief is the
    honest fallback; a materially-hollow narrow prompt is not."""
    assert task_runner._declared_agentic_prompt(
        "verify_plan", _Task(), [], plan=None) == ""
    assert task_runner._declared_agentic_prompt(
        "verify_plan", _Task(), [], plan={"prompt": ""}) == ""


def test_a_narrow_verify_plan_adopts_the_declared_caps():
    """narrow=True is what unlocks the declared turn/spend caps; the
    allowlist entry alone would leave the runner defaults in place."""
    plan = task_runner._node_plan("prism", "verify_plan")
    narrow = task_runner._invoke_budget("verify_plan", plan, narrow=True)
    wide = task_runner._invoke_budget("verify_plan", plan, narrow=False)

    assert narrow["max_turns"] == 4
    assert narrow["max_budget_usd"] == 0.5
    assert wide["max_turns"] != 4, (
        "without the declared prompt the declared caps must NOT apply "
        "(task 6a7105f9: they kill the step)")


# ----------------------------------------------------------------------
# AC-3 -- the proof is split into the two fields the rubric reads
# ----------------------------------------------------------------------

_REPORT = """## Plan

Covers AC-1 and AC-2.

```mermaid
flowchart TD
  A[ship_worker land] --> B[reap]
  C[hand push] --> B
```

Then the reap runs.
"""


class _RecordingSvc:
    def __init__(self):
        self.fields = {}

    def update(self, task_id, **kw):
        self.fields.update(kw)


def test_verify_plan_fills_plan_diagram_as_well_as_plan_doc():
    """arc_governance.py:279-285 reads the plan_diagram FIELD. A report whose
    mermaid lives only inside plan_doc is a gate refusal, not a plan."""
    svc = _RecordingSvc()
    task_runner._route_proof(svc, "t-1", "verify_plan", _REPORT)

    assert "AC-1" in svc.fields["plan_doc"], "coverage is diffed off plan_doc"
    diagram = svc.fields.get("plan_diagram") or ""
    assert diagram.strip(), "plan_diagram must not be empty"
    assert diagram.lstrip().startswith("flowchart"), (
        "the field carries mermaid SOURCE, not a fenced markdown block")
    assert "```" not in diagram, "the fence markers must be stripped"


def test_a_report_with_no_diagram_leaves_plan_diagram_untouched():
    """Never invent a diagram. An absent one must stay absent so the rubric
    can refuse honestly, rather than pass on a fabricated field."""
    svc = _RecordingSvc()
    task_runner._route_proof(svc, "t-1", "verify_plan", "## Plan\n\nAC-1 only.")

    assert "AC-1" in svc.fields["plan_doc"]
    assert not (svc.fields.get("plan_diagram") or "").strip()


def test_draft_story_routing_is_unchanged():
    """draft_story is also a _PLAN_STEPS member and writes a STORY. It must
    not gain a plan_diagram."""
    svc = _RecordingSvc()
    task_runner._route_proof(svc, "t-1", "draft_story", _REPORT)

    assert svc.fields["plan_doc"] == _REPORT
    assert "plan_diagram" not in svc.fields


# ----------------------------------------------------------------------
# AC-4 -- write_failing_tests gets the same prompt dispatch as verify_plan
# ----------------------------------------------------------------------

def test_write_failing_tests_is_a_planned_step():
    """Regression: write_failing_tests must be in _PLANNED_STEPS so that
    _declared_agentic_prompt can dispatch to it and enable narrow_prompt."""
    assert "write_failing_tests" in task_runner._PLANNED_STEPS
    plan = task_runner._node_plan("prism", "write_failing_tests")
    assert plan is not None, "write-failing-tests-loop.json declares a real plan"


def test_write_failing_tests_sends_the_declared_narrow_prompt():
    """Regression: _declared_agentic_prompt must return a non-empty prompt
    for write_failing_tests when given a plan with a prompt body and a task
    with material."""
    plan = task_runner._node_plan("prism", "write_failing_tests")
    prompt = task_runner._declared_agentic_prompt(
        "write_failing_tests", _Task(), [], plan=plan)

    assert prompt, (
        "write_failing_tests must have a narrow prompt, not the step brief; "
        "the dispatch gate short-circuits when narrow_prompt is empty")
    assert _Task.title in prompt, "the task material is substituted in"
    assert "${taskHint}" not in prompt, "the placeholder must be filled"


def test_write_failing_tests_falls_back_to_full_brief_with_no_declaration():
    """Same guard as verify_plan: no plan or empty prompt = fall back."""
    assert task_runner._declared_agentic_prompt(
        "write_failing_tests", _Task(), [], plan=None) == ""
    assert task_runner._declared_agentic_prompt(
        "write_failing_tests", _Task(), [], plan={"prompt": ""}) == ""
