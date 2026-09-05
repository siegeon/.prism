"""The runner runs the node's DECLARED agentic middle, not a monolith.

THE OVERRUN AND THE EMPTY CANVAS, one cause. review-previous-notes-loop
declares a bounded middle: premise-judge, haiku, 2 turns, $0.50, and no
tools -- because `gather` already resolved every citation it needs. The
runner instead sent job["instructions"] (the entire step brief) with 30
turns and the full BUILD_TOOLS set, so:

  * the declared caps could not be honoured and a step the node sizes at
    ~2 minutes ran for an hour or more (durations recorded live 2026-09-05:
    7,334,463 ms / 6,003,890 ms / 4,123,014 ms), which the canvas rendered
    as OVERRUN 3956.2x; and
  * the declared chain never executed AS STEPS, so every sub-node on the
    Workflows canvas read "too few runs (0/20)" -- real work, entirely
    invisible, which is the honest reason the board could not say what was
    going on.

_invoke_budget's own note set the condition for this change: "a future
slice that also adopts the declared PROMPT may then adopt the caps that
were written for it -- never one without the other." These pin exactly
that pairing, and the negative case that made the rule: caps must NOT be
adopted when the full brief is still what gets sent.
"""
from __future__ import annotations

import types

import pytest

from prism_service.services import task_runner as tr
from prism_service.services.premise_gather import GatheredFact

PLAN = {"model": "haiku", "max_turns": 2, "max_budget_usd": 0.5,
        "codified": ["premise-gather", "premise-render",
                     "premise-citation-check", "text-challenge"],
        "agentic": "premise-judge"}


def _facts():
    return [GatheredFact(kind="memory", text="The runner drives one task per tick",
                         citation="`memory_recall(\"runner\") -> project-runner`")]


def _task():
    return types.SimpleNamespace(id="t-1", title="A task", description="does a thing",
                                 oracle="")


# --- the declared prompt --------------------------------------------------

def test_the_narrow_prompt_carries_the_gathered_citations():
    p = tr._declared_agentic_prompt("review_previous_notes", _task(), _facts())

    assert p, "the premise step with facts must get the node's narrow prompt"
    assert "## Premises" in p
    assert "reusing its citation VERBATIM" in p
    assert "project-runner" in p, "the gathered citation must be IN the prompt"
    # It must be the narrow brief, not the whole step instruction set.
    assert "Gathered material:" in p


def test_no_facts_means_no_narrow_prompt():
    """With nothing gathered the narrow prompt has no material, so the full
    brief stays the honest fallback rather than a model being asked to judge
    an empty list."""
    assert tr._declared_agentic_prompt("review_previous_notes", _task(), []) == ""


def test_another_step_is_untouched():
    """Only the step whose node declares this middle changes."""
    assert tr._declared_agentic_prompt("implement_tasks", _task(), _facts()) == ""


# --- the caps travel WITH the prompt, never alone -------------------------

def test_the_declared_caps_are_adopted_with_the_declared_prompt():
    b = tr._invoke_budget("review_previous_notes", PLAN, narrow=True)

    assert b["max_turns"] == 2, b
    assert b["max_budget_usd"] == 0.5, b
    assert b["model"] == "haiku", b


def test_the_declared_caps_are_REFUSED_without_the_declared_prompt():
    """The rule the old note was written to protect: task 6a7105f9 blocked
    three times when max_turns=2 was applied to the FULL brief and each
    attempt died mid-turn. Caps without the prompt must keep the runner's
    own limits."""
    b = tr._invoke_budget("review_previous_notes", PLAN, narrow=False)

    assert b["max_turns"] == tr._max_turns(), b
    assert b["max_turns"] != 2, b
    assert b["max_budget_usd"] == tr._max_budget_usd(), b
    # the model is still safe to adopt either way
    assert b["model"] == "haiku", b


def test_a_step_with_no_plan_keeps_the_runner_defaults():
    b = tr._invoke_budget("review_previous_notes", None, narrow=False)
    assert b["max_turns"] == tr._max_turns()
    assert b["model"] == ""


# --- the plan reports which route is the middle ---------------------------

def test_the_plan_names_its_agentic_route(tmp_path, monkeypatch):
    """_node_plan must report WHICH route is the agentic middle -- without
    it the runner cannot record the declared sub-step, which is what left
    the canvas reading 0/20."""
    import json

    behaviors = tmp_path / ".prism" / "behaviors" / "conductor"
    behaviors.mkdir(parents=True)
    (behaviors / "review-previous-notes-loop.json").write_text(json.dumps({
        "id": "review-previous-notes-loop", "steps": [
            {"id": "gather", "kind": "http-callback",
             "url": "${u}/api/workflows/steps/premise-gather?project=p", "body": "{}"},
            {"id": "loop", "kind": "http-callback",
             "url": "${u}/api/workflows/steps/premise-judge?project=p",
             "body": json.dumps({"model": "haiku", "max_turns": 2,
                                 "max_budget_usd": 0.5})},
            {"id": "render", "kind": "http-callback",
             "url": "${u}/api/workflows/steps/premise-render?project=p", "body": "{}"},
        ]}), encoding="utf-8")
    monkeypatch.setattr(tr, "_behavior_dir", lambda project: behaviors)

    plan = tr._node_plan("prism", "review_previous_notes")

    assert plan is not None
    assert plan["agentic"] == "premise-judge", plan
    assert "premise-render" in plan["codified"], plan
    assert plan["max_turns"] == 2 and plan["max_budget_usd"] == 0.5, plan


# --- THE POINT: the model is the LAST resort, not the route --------------

def test_the_premise_step_resolves_with_no_model_at_all():
    """gather -> render -> check is a COMPLETE deterministic chain. When it
    produces a section the rubric accepts, the step is finished with zero
    tokens and no `claude -p` at all."""
    task = types.SimpleNamespace(
        id="t-1", title="A task", description="",
        oracle="(1) The runner drives one task per tick.")

    proof = tr._codified_step_proof("review_previous_notes", task, _facts())

    assert proof, "the deterministic chain must resolve this step by itself"
    assert "## Premises" in proof
    assert "project-runner" in proof


def test_the_codified_result_reports_zero_cost_honestly():
    r = tr._CodifiedResult("## Premises\n- a (x.py:1)\n")

    assert r.exit_code == 0
    assert r.usage is None, "a step that called no model must not report usage"
    assert r.final_text().startswith("## Premises")
    assert r.graceful_budget_stop() is False


def test_a_section_the_rubric_would_refuse_is_NOT_taken():
    """The shortcut may never advance a step on evidence the gate would
    reject: an oracle clause nothing engages must still be marked, and if
    the render cannot satisfy both teeth the model gets the work."""
    task = types.SimpleNamespace(id="t-1", title="A task", description="",
                                 oracle="(1) x.")
    assert tr._codified_step_proof("review_previous_notes", task, []) == ""


def test_other_steps_never_take_the_shortcut():
    task = types.SimpleNamespace(id="t-1", title="A task", description="", oracle="")
    assert tr._codified_step_proof("implement_tasks", task, _facts()) == ""
