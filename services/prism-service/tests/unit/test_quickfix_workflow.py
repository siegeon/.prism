"""Task 811fcce0: the quickfix workflow's own step-shape regression guard.

Same posture as test_conductor_walks_task_workflow.py pins for triage
(task 6f22d0ad) -- a source-level pin on QUICKFIX_STEPS' shape, order, and
registration, so a later edit cannot silently reintroduce the
POLICY_FILE problem align_language's own doc comment warns against:
wiring a real conductor_service.py rubric into a step whose whole point
is to stay outside that file. The last assertion is the regression guard
named in the task brief -- every step's validation is None.
"""

from __future__ import annotations


def test_quickfix_steps_are_four_in_order_with_the_right_types_and_agents():
    from prism_service.models.workflow import QUICKFIX_STEPS

    assert len(QUICKFIX_STEPS) == 4
    assert [s["id"] for s in QUICKFIX_STEPS] == [
        "intake", "apply_fix", "verify_fix", "done",
    ]
    assert [s["type"] for s in QUICKFIX_STEPS] == [
        "intake", "agent", "agent", "done",
    ]
    # apply_fix is the ONE agentic step (role dev); verify_fix is
    # deterministic (agent=None), same as intake/done -- a real pytest
    # subprocess check, never an LLM judgment call (see the doc comment
    # above QUICKFIX_STEPS in models/workflow.py).
    assert [s["agent"] for s in QUICKFIX_STEPS] == [
        None, "dev", None, None,
    ]


def test_quickfix_is_registered_and_steps_for_resolves_it():
    from prism_service.models.workflow import QUICKFIX_STEPS, WORKFLOWS, steps_for

    assert "quickfix" in WORKFLOWS
    assert WORKFLOWS["quickfix"] == QUICKFIX_STEPS
    assert steps_for("quickfix") == QUICKFIX_STEPS
    assert steps_for("QuickFix") == QUICKFIX_STEPS  # normalized, case-insensitive


def test_quickfix_steps_never_carry_a_validation_rubric():
    """Regression guard named in the task brief: adding a real rubric here
    would need a new _VERIFIER_RULES entry AND touch conductor_service.py,
    a control_plane.POLICY_FILES entry this workflow must never edit --
    same reasoning as align_language's own doc comment."""
    from prism_service.models.workflow import QUICKFIX_STEPS

    for step in QUICKFIX_STEPS:
        assert step["validation"] is None, step


def test_quickfix_has_no_gate_step_at_all():
    from prism_service.models.workflow import QUICKFIX_STEPS

    assert all(s["type"] != "gate" for s in QUICKFIX_STEPS)


def test_known_workflows_and_validate_workflow_accept_quickfix():
    from prism_service.models.task import known_workflows, validate_workflow

    assert "quickfix" in known_workflows()
    assert validate_workflow("quickfix") == "quickfix"
