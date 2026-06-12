"""Workflow state model and step definitions for PRISM."""

from __future__ import annotations

from dataclasses import dataclass, field


# Gates inherit their validation kind from the nearest PRECEDING agent
# step's `validation` (ConductorService._validation_for_gate). story_gate
# and plan_gate (task 8579d49e) make the story_complete / plan_coverage
# rubrics REACHABLE end-to-end: both are scored by pure YAML-rubric
# functions (services/arc_governance.py) — no override required on a
# compliant drive.
WORKFLOW_STEPS = [
    {"id": "review_previous_notes", "agent": "sm", "type": "agent", "validation": None},
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
