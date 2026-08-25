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

# Triage workflow (task b837bc98): a second, deliberately SHORT named
# workflow beside the 10-step implement/conductor SDLC above -- for an
# item that only needs a bucket and a single owner decision, not a story/
# plan/red/green loop. classify is the only agent step (role sm: bucket
# Open/Monitoring/Resolved/Dropped with a one-line reason); decide is the
# ONLY gate -- the single human/owner stop this workflow has, matching the
# "at most one owner stop" shape a triage bucket actually needs.
TRIAGE_STEPS = [
    {"id": "intake", "agent": None, "type": "intake", "validation": None},
    {"id": "classify", "agent": "sm", "type": "agent", "validation": "triage_bucketed"},
    {"id": "decide", "agent": None, "type": "gate", "validation": None},
    {"id": "done", "agent": None, "type": "done", "validation": None},
]

# NAMED workflow registry (task b837bc98): every step list a task can be
# driven by, keyed by the SAME worker-facing value models.task.Task.workflow
# stores ("implement" is models.task.DEFAULT_WORKFLOW). WORKFLOW_STEPS above
# stays the literal, byte-for-byte "implement" entry -- existing callers that
# import WORKFLOW_STEPS directly (services/conductor_service.py's per-task
# walk, api/workflows.py's own top-level `steps`) are unaffected by adding a
# second workflow here.
WORKFLOWS: dict[str, list[dict]] = {
    "implement": WORKFLOW_STEPS,
    "triage": TRIAGE_STEPS,
}


def steps_for(workflow: str) -> list[dict]:
    """Resolve a task's workflow value to its ordered step list.

    Checks WORKFLOWS directly first ("implement", "triage" -- the same
    worker-facing values models.task.Task.workflow stores). Also reuses
    models.task.WORKFLOW_ALIASES (a value -> catalog-id map, e.g.
    "implement" -> "conductor") so a catalog id resolves too, the same join
    api/workflows.py's _task_count_by_workflow already performs. Any other
    value -- unknown, or blank -- falls back to the implement workflow's
    steps, mirroring models.task.normalize_workflow's own default. Never
    raises."""
    from prism_service.models.task import WORKFLOW_ALIASES

    value = (workflow or "").strip().lower()
    if value in WORKFLOWS:
        return WORKFLOWS[value]
    for alias, catalog_id in WORKFLOW_ALIASES.items():
        if catalog_id == value and alias in WORKFLOWS:
            return WORKFLOWS[alias]
    return WORKFLOWS["implement"]


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
