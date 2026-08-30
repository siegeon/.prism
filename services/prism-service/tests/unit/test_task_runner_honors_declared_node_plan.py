"""The drive worker executes a node's DECLARED plan (task 8848089d).

Every conductor node declares an execution plan in
`.prism/behaviors/conductor/<behavior>.json`: which model, how many turns,
what budget, and which sub-steps are CODIFIED — deterministic Python that
costs no tokens.

`task_runner._run_one_step` used to ignore all of it and fire ONE
`claude_cli.invoke` per step with the blanket 30-turn / $2.00 / 900 s
defaults, so `services/premise_gather.py` (task cd33263f) had no caller
outside its own unit tests, and `review_previous_notes` — the FIRST step of
every task — grepped the repo cold on every drive: 3,762 tokens and a
1,725 s mean against a declared 180 s bound.

These tests pin the plan reader and the codified dispatch. They assert the
LITERAL declared figures (haiku / 2 turns / $0.50), never the reader applied
to its own output.
"""

from __future__ import annotations

import json

import pytest

from prism_service.services import task_runner


# ----------------------------------------------------------------------
# _node_plan — reads the declared plan off the behavior JSON
# ----------------------------------------------------------------------

def test_review_previous_notes_plan_carries_its_declared_budget():
    """The plan reports the figures review-previous-notes-loop.json declares.

    Literal expected values, not `_node_plan(...)` compared to itself: the
    behavior file says haiku / 2 turns / $0.50, and a reader that silently
    fell back to the runner's 30-turn default would still pass a
    self-referential assertion.
    """
    plan = task_runner._node_plan("prism", "review_previous_notes")

    assert plan is not None, "review_previous_notes declares a plan"
    assert plan["model"] == "haiku"
    assert plan["max_turns"] == 2
    assert plan["max_budget_usd"] == pytest.approx(0.5)


def test_review_previous_notes_plan_names_its_codified_substeps():
    """gather and check are codified; only the middle judge calls a model."""
    plan = task_runner._node_plan("prism", "review_previous_notes")

    assert plan["codified"] == ["premise-gather", "premise-citation-check"]


@pytest.mark.parametrize("step", ["implement_tasks", "verify_green_state",
                                  "write_failing_tests"])
def test_build_steps_keep_the_runner_budget(step):
    """A build step is NOT governed by the template budget.

    The five non-premise agentic behaviors all carry the SAME boilerplate
    (haiku / 4 turns / $0.50 / 120 s), which is a template rather than a
    considered figure — `implement_tasks` alone has a 474 s median and
    needs far more than four turns. Honouring it would break every build
    step, so only a node with a real hand-tuned declaration opts in.
    """
    assert task_runner._node_plan("prism", step) is None


def test_a_step_with_no_behavior_file_has_no_plan():
    assert task_runner._node_plan("prism", "not_a_real_step") is None


# ----------------------------------------------------------------------
# _codified_preamble — premise_gather runs as Python, no model call
# ----------------------------------------------------------------------

class _Task:
    id = "t-1"
    title = "A node's declared plan governs its drive"
    description = "The runner reads .prism/behaviors/conductor."
    tags: list[str] = []


def test_codified_preamble_resolves_facts_without_a_model(monkeypatch):
    """The gather sub-step is pure retrieval — it must never invoke claude."""
    from prism_service.services import premise_gather

    called = []
    monkeypatch.setattr(
        task_runner, "_invoke_model",
        lambda *a, **k: called.append(a) or None, raising=False)
    monkeypatch.setattr(
        premise_gather, "gather",
        lambda *a, **k: [premise_gather.GatheredFact(
            kind="memory", text="The runner ignored the declared plan.",
            citation="services/task_runner.py:656")])

    preamble, facts = task_runner._codified_preamble(
        "prism", _Task(), memory_svc=None, task_svc=None, brain_svc=None)

    assert not called, "gather must not call a model"
    assert len(facts) == 1
    assert "services/task_runner.py:656" in preamble


def test_codified_preamble_is_empty_when_nothing_is_found(monkeypatch):
    """No facts means no preamble — never an empty heading with no content."""
    from prism_service.services import premise_gather
    monkeypatch.setattr(premise_gather, "gather", lambda *a, **k: [])

    preamble, facts = task_runner._codified_preamble(
        "prism", _Task(), memory_svc=None, task_svc=None, brain_svc=None)

    assert preamble == ""
    assert facts == []


def test_codified_preamble_survives_a_gather_failure(monkeypatch):
    """A broken gather degrades to the plain prompt, never kills the drive."""
    from prism_service.services import premise_gather

    def _boom(*a, **k):
        raise RuntimeError("brain unavailable")

    monkeypatch.setattr(premise_gather, "gather", _boom)

    preamble, facts = task_runner._codified_preamble(
        "prism", _Task(), memory_svc=None, task_svc=None, brain_svc=None)

    assert preamble == ""
    assert facts == []


# ----------------------------------------------------------------------
# The declared plan reaches claude_cli.invoke
# ----------------------------------------------------------------------

def test_declared_plan_pins_the_model_only():
    """The declared MODEL applies; the declared caps do not.

    A behavior's turn/budget caps are sized for its own narrow prompt.
    This worker still sends the full step brief, so adopting them starves
    it -- see test_a_declared_plan_never_shrinks_the_turn_budget below for
    the live incident this encodes.
    """
    plan = task_runner._node_plan("prism", "review_previous_notes")
    kwargs = task_runner._invoke_budget("review_previous_notes", plan)

    assert kwargs["model"] == "haiku"
    assert kwargs["max_turns"] == task_runner._max_turns()
    assert kwargs["max_budget_usd"] == pytest.approx(
        task_runner._max_budget_usd())


def test_an_unplanned_step_keeps_every_runner_default():
    """No plan means the runner's own budget, and no model pin.

    The empty string is claude_cli.invoke's own default for `model`
    (inference/claude_cli.py:284) -- None is not a valid value there.
    """
    kwargs = task_runner._invoke_budget("implement_tasks", None)

    assert kwargs["model"] == ""
    assert kwargs["max_turns"] == task_runner._max_turns()
    assert kwargs["max_budget_usd"] == pytest.approx(
        task_runner._max_budget_usd())
    assert kwargs["timeout_s"] == pytest.approx(
        task_runner._step_timeout_s("implement_tasks"))


def test_a_declared_plan_never_shortens_the_wall_clock():
    """The declared 120 s bound must not cap a step the runner allows longer.

    A too-small timeout kills a drive outright, while a too-large one costs
    nothing once the turn and budget caps bind first — so the timeout floor
    stays the runner's.
    """
    plan = task_runner._node_plan("prism", "review_previous_notes")
    kwargs = task_runner._invoke_budget("review_previous_notes", plan)

    assert kwargs["timeout_s"] >= task_runner._step_timeout_s(
        "review_previous_notes")


# ----------------------------------------------------------------------
# The codified sub-step reports its own zero-token run
# ----------------------------------------------------------------------

def test_codified_substep_records_a_zero_token_run(tmp_path):
    """A codified sub-step is visible on the Workflows board at 0 tokens.

    Without its own agent_runs row the saving is invisible: the node card
    would keep reporting only the agentic middle and nobody could tell the
    gather ever ran.
    """
    from prism_service.services import agent_runs_data

    db = str(tmp_path / "scores.db")
    agent_runs_data.upsert_agent_run(db, {
        "run_id": "r-1", "task_id": "t-1", "step": "premise-gather",
        "agent_id": "prism-task-runner", "role": "sm", "tokens": 0,
        "ok": 1,
    })

    rows = agent_runs_data.get_agent_runs(db, task_id="t-1")
    gather = [r for r in rows if r["step"] == "premise-gather"]

    assert len(gather) == 1
    assert gather[0]["tokens"] == 0


def test_behavior_json_on_disk_still_declares_the_premise_split():
    """Guards the contract this wiring reads.

    If someone flattens review-previous-notes-loop back to a single
    reason-loop callback, the runner silently loses its codified sub-steps
    and the token cut evaporates with no test going red anywhere else.
    """
    path = (task_runner._behavior_dir("prism")
            / "review-previous-notes-loop.json")
    assert path.exists(), f"missing behavior file: {path}"

    doc = json.loads(path.read_text())
    routes = [s.get("url", "").split("/steps/")[-1].split("?")[0]
              for s in doc["steps"]]

    assert routes == ["premise-gather", "premise-judge",
                      "premise-citation-check"]


# ----------------------------------------------------------------------
# THE WIRING — _run_one_step must actually call the above
# ----------------------------------------------------------------------
# The whole defect this task fixes was a set of codified endpoints with no
# caller outside their own unit tests. Testing the helpers in isolation
# would reproduce exactly that: every helper green, the drive unchanged.
# These two pin the call itself.

class _Result:
    exit_code = 0
    run_id = "r-live"
    usage = {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01,
             "model": "haiku"}

    def final_text(self):
        return "## premises\n- The runner ignored the plan (task_runner.py:656)"

    def graceful_budget_stop(self):
        return False


def _drive_once(monkeypatch, step, tmp_path):
    """Run _run_one_step for `step`, returning the invoke kwargs it used."""
    from prism_service.api import conductor_flow as flow
    from prism_service.inference import claude_cli
    from prism_service.services import task_workspace, premise_gather

    seen = {}

    monkeypatch.setattr(flow, "flow_start", lambda *a, **k: {
        "ok": True, "job": {"step": step, "kind": "agent",
                            "instructions": "DO THE STEP"}})
    monkeypatch.setattr(flow, "flow_report",
                        lambda *a, **k: {"ok": True, "advanced": True})
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(tmp_path)})
    monkeypatch.setattr(task_runner, "_stall_count", lambda *a, **k: 0)
    monkeypatch.setattr(task_runner, "_route_proof", lambda *a, **k: None)
    monkeypatch.setattr(premise_gather, "gather", lambda *a, **k: [
        premise_gather.GatheredFact(
            kind="memory", text="The declared plan was ignored.",
            citation="services/task_runner.py:656")])

    def _fake_invoke(prompt, **kwargs):
        seen["prompt"] = prompt
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)
    task_runner._run_one_step("prism", "t-live")
    return seen


def test_run_one_step_applies_the_declared_model(monkeypatch, tmp_path):
    """review_previous_notes reaches claude_cli on haiku, with full turns.

    The declared model is the saving that is safe to take; the declared
    2-turn cap is not (see test_a_declared_plan_never_shrinks_the_turn_
    budget).
    """
    seen = _drive_once(monkeypatch, "review_previous_notes", tmp_path)

    assert seen["model"] == "haiku"
    assert seen["max_turns"] == task_runner._max_turns()


def test_run_one_step_prepends_the_codified_facts(monkeypatch, tmp_path):
    """The judge is handed resolved citations instead of grepping cold."""
    seen = _drive_once(monkeypatch, "review_previous_notes", tmp_path)

    assert "services/task_runner.py:656" in seen["prompt"]
    assert seen["prompt"].endswith("DO THE STEP")


def test_run_one_step_leaves_a_build_step_alone(monkeypatch, tmp_path):
    """implement_tasks keeps the runner's own budget and an unmodified prompt."""
    seen = _drive_once(monkeypatch, "implement_tasks", tmp_path)

    assert seen["model"] == ""
    assert seen["max_turns"] == task_runner._max_turns()
    assert seen["prompt"] == "DO THE STEP"


def test_a_declared_plan_never_shrinks_the_turn_budget():
    """REGRESSION, task 6a7105f9 (2026-08-30).

    7.13.203 applied review-previous-notes-loop's declared max_turns=2 to
    the FULL step brief. The first task the daemon drove afterwards spent
    its two turns on "Let me fetch the task details and any prior notes to
    review.", died `exit=1 ... truncated mid-turn` three times, and the
    task blocked at review_previous_notes -- which would have happened to
    EVERY task, since this is the first step of every drive.

    A declared budget must never leave a step with fewer turns or less
    money than the runner would have given it.
    """
    for step in ("review_previous_notes", "draft_story", "implement_tasks"):
        plan = task_runner._node_plan("prism", step)
        kwargs = task_runner._invoke_budget(step, plan)
        assert kwargs["max_turns"] >= task_runner._max_turns(), step
        assert kwargs["max_budget_usd"] >= task_runner._max_budget_usd(), step


def test_the_model_reaches_invoke_as_a_string(monkeypatch, tmp_path):
    """claude_cli.invoke types `model` as str, never None."""
    seen = _drive_once(monkeypatch, "implement_tasks", tmp_path)
    assert isinstance(seen["model"], str)
