"""Workflow state model and step definitions for PRISM."""

from __future__ import annotations

from dataclasses import dataclass, field


# Gates inherit their validation kind from the nearest PRECEDING agent
# step's `validation` (ConductorService._validation_for_gate). story_gate
# and plan_gate (task 8579d49e) make the story_complete / plan_coverage
# rubrics REACHABLE end-to-end: both are scored by pure YAML-rubric
# functions (services/arc_governance.py) — no override required on a
# compliant drive.
#
# review_previous_notes's premise_grounded (task 3a63190b / issue #222) is
# DIFFERENT: because draft_story sits directly after it and already owns
# validation="story_complete", gate inheritance would always resolve
# story_gate to story_complete first — premise_grounded can never reach a
# gate that way. It is instead checked AT the review_previous_notes step
# itself, by ConductorService.advance_task (see _VERIFIER_RULES's
# "check_at_step" flag), before the review_previous_notes -> draft_story
# transition is allowed.
WORKFLOW_STEPS = [
    {"id": "review_previous_notes", "agent": "sm", "type": "agent", "validation": "premise_grounded"},
    {"id": "draft_story", "agent": "sm", "type": "agent", "validation": "story_complete"},
    {"id": "story_gate", "agent": None, "type": "gate", "validation": None},
    {"id": "verify_plan", "agent": "sm", "type": "agent", "validation": "plan_coverage"},
    {"id": "plan_gate", "agent": None, "type": "gate", "validation": None},
    {"id": "write_failing_tests", "agent": "qa", "type": "agent", "validation": "red_with_trace"},
    {"id": "red_gate", "agent": None, "type": "gate", "validation": None},
    {"id": "implement_tasks", "agent": "dev", "type": "agent", "validation": "green"},
    {"id": "verify_green_state", "agent": "qa", "type": "agent", "validation": "green_full"},
    {"id": "green_gate", "agent": None, "type": "gate", "validation": None},
]


@dataclass
class WorkflowState:
    """Current state of the PRISM workflow engine."""

    active: bool = False
    workflow: str = ""
    current_step: str = ""
    current_step_index: int = 0
    total_steps: int = len(WORKFLOW_STEPS)
    story_file: str = ""
    paused_for_manual: bool = False
    session_id: str = ""
    model: str = ""
    total_tokens: int = 0
    step_history: list[dict] = field(default_factory=list)
