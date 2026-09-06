"""Archify map builder: one task's workflow, concepts, and children."""

from __future__ import annotations

from prism_service.models.workflow import (
    WORKFLOW_STEPS, TRIAGE_STEPS, ALIGN_LANGUAGE_STEPS,
    PROMOTE_TO_LAW_STEPS, QUICKFIX_STEPS
)
from prism_service.project_context import get_project
from prism_service.services.archify_maps._layout import slug, clip
from prism_service.services.okf_host import OkfHost

DIAGRAM_TYPE = "workflow"

# The workflow schema bounds a node's column, so EVERY lane is capped at the
# same width. Capping the step lane alone let a longer concept or child lane
# run past the last column and the map failed to validate.
_MAX_COLS = 6

# Workflow name to steps mapping
_WORKFLOW_STEPS_MAP = {
    "implement": WORKFLOW_STEPS,
    "triage": TRIAGE_STEPS,
    "align_language": ALIGN_LANGUAGE_STEPS,
    "promote_to_law": PROMOTE_TO_LAW_STEPS,
    "quickfix": QUICKFIX_STEPS,
}

# Phase grouping: step_id -> phase info
_PHASE_MAP = {
    # implement workflow
    "review_previous_notes": ("Intake", 0),
    "draft_story": ("Intake", 0),
    "story_gate": ("Intake", 0),
    "verify_plan": ("Planning", 1),
    "plan_gate": ("Planning", 1),
    "write_failing_tests": ("Build", 2),
    "red_gate": ("Build", 2),
    "implement_tasks": ("Build", 2),
    "verify_green_state": ("Verify", 3),
    "green_gate": ("Verify", 3),
    # triage workflow
    "intake": ("Intake", 0),
    "classify": ("Process", 1),
    "decide": ("Review", 2),
    "done": ("Done", 3),
    # align_language workflow
    "collect": ("Collect", 0),
    "align": ("Align", 1),
    "verify": ("Verify", 2),
    # promote_to_law workflow
    "draft": ("Draft", 0),
    "review": ("Review", 1),
    "install": ("Install", 2),
    # quickfix workflow
    "apply_fix": ("Apply", 1),
    "verify_fix": ("Verify", 2),
}


def _window(steps: list, current_id, width: int) -> tuple[list, int]:
    """The `width` steps to draw, centred on the step the task is on.

    Returns (steps, index_of_first). The whole list is returned unchanged when
    it already fits.
    """
    if len(steps) <= width:
        return steps, 0
    try:
        here = next(i for i, s in enumerate(steps) if s.get("id") == current_id)
    except StopIteration:
        here = 0
    start = max(0, min(here - width // 2, len(steps) - width))
    return steps[start:start + width], start


def build(project: str, *, task_id: str | None = None) -> dict:
    """Build the task workflow map: steps, concepts, and children."""
    if not task_id:
        raise ValueError("task_id is required")

    try:
        ctx = get_project(project)
        task_svc = ctx.task_svc
        memory_svc = ctx.memory_svc
    except Exception as e:
        raise ValueError(f"Project context failed: {e}")

    # Fetch the task
    task = task_svc.get(task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")

    # Get the workflow steps for this task
    workflow_name = getattr(task, "workflow", None) or "implement"
    steps = _WORKFLOW_STEPS_MAP.get(workflow_name, WORKFLOW_STEPS)

    # Get concepts (knowledge)
    okf = OkfHost(memory_svc, project)
    concepts = okf.task_concepts(task_id)

    # Get child tasks
    children = task_svc.list(parent_id=task_id)

    # The workflow schema allows six columns, and the implement workflow has
    # ten steps. Show the window AROUND the step the task is on rather than
    # the first six: a task at green_gate would otherwise render only the
    # opening steps and hide where the work actually is.
    _total_steps = len(steps)
    steps, window_from = _window(steps, task.workflow_step, _MAX_COLS)

    # Determine layout dimensions (workflow schema limits cols to 0-5, so max 6 items per lane)
    step_count = min(len(steps), _MAX_COLS)
    concept_count = min(len(concepts), _MAX_COLS)
    child_count = min(len(children), _MAX_COLS)
    max_col = max(step_count - 1, concept_count - 1, child_count - 1, 0) + 1

    # Build workflow step nodes (lane: "flow")
    nodes = []
    edges = []
    step_ids = []

    for step_idx, step in enumerate(steps[:_MAX_COLS]):
        step_id = step["id"]
        step_ids.append(step_id)

        # Mark current step and completed steps
        tag = None
        variant = None
        if task.workflow_step == step_id:
            tag = "current"
            variant = "emphasis"
        elif _is_step_completed(steps, task.workflow_step, step_id):
            tag = "done"

        node = {
            "id": slug(f"step-{step_id}"),
            "lane": "flow",
            "col": step_idx,
            "type": "backend" if step.get("type") == "agent" else "external",
            "label": clip(_step_label(step_id), 16),
            "width": 140,
        }
        if tag:
            node["tag"] = tag
        nodes.append(node)

    # Build concept nodes (lane: "knowledge") — cap at 8
    concept_start_col = step_count + 1 if concept_count > 0 else step_count
    for concept_idx, concept in enumerate(concepts[:_MAX_COLS]):
        concept_id = slug(f"concept-{concept['id']}")
        node = {
            "id": concept_id,
            "lane": "knowledge",
            "col": concept_idx,
            "type": "cloud",
            "label": clip(concept["title"], 16),
            "sublabel": clip(concept.get("domain", ""), 16),
            "width": 140,
        }
        nodes.append(node)

    # Build child task nodes (lane: "work") — cap at 8
    for child_idx, child in enumerate(children[:_MAX_COLS]):
        child_id_str = child.id if hasattr(child, "id") else child.get("id", "")
        child_title = child.title if hasattr(child, "title") else child.get("title", "Task")
        child_status = child.status if hasattr(child, "status") else child.get("status", "pending")

        node = {
            "id": slug(f"child-{child_id_str}"),
            "lane": "work",
            "col": child_idx,
            "type": "external",
            "label": clip(child_title, 16),
            "sublabel": clip(str(child_status), 16),
            "width": 140,
        }
        nodes.append(node)

    # Build edges: sequential steps
    for i in range(len(step_ids) - 1):
        edges.append({
            "from": slug(f"step-{step_ids[i]}"),
            "to": slug(f"step-{step_ids[i + 1]}"),
            "variant": "default",
        })

    # Build edges: concepts to first step node (if they exist)
    if concepts and step_ids:
        first_step_id = slug(f"step-{step_ids[0]}")
        for concept in concepts[:_MAX_COLS]:
            concept_id = slug(f"concept-{concept['id']}")
            edges.append({
                "from": concept_id,
                "to": first_step_id,
                "variant": "default",
            })

    # Build phases grouping
    phases = []
    seen_phases = {}
    for step_idx, step in enumerate(steps[:_MAX_COLS]):
        phase_name, phase_idx = _PHASE_MAP.get(step["id"], ("Other", 99))
        if phase_name not in seen_phases:
            seen_phases[phase_name] = {
                "id": slug(phase_name),
                "label": phase_name,
                "fromCol": step_idx,
                "toCol": step_idx,
            }
        else:
            seen_phases[phase_name]["toCol"] = step_idx
    phases = list(seen_phases.values())

    # Main path = ordered step ids
    main_path = [slug(f"step-{sid}") for sid in step_ids]

    # Build cards
    cards = [
        {
            "dot": "cyan",
            "title": "Workflow",
            "items": [
                (f"Steps {window_from + 1} to {window_from + len(step_ids)} "
                 f"of {_total_steps}, around the current one."
                 if _total_steps > len(step_ids)
                 else f"All {len(step_ids)} steps of the workflow."),
                f"Current step: {_step_label(task.workflow_step)}" if task.workflow_step else "No step assigned.",
            ],
        },
        {
            "dot": "emerald" if concept_count > 0 else "slate",
            "title": "Knowledge",
            "items": [
                f"{len(concepts)} concepts recalled.",
            ] if concept_count > 0 else ["No concepts recalled."],
        },
        {
            "dot": "violet" if child_count > 0 else "slate",
            "title": "Children",
            "items": [
                f"{len(children)} child tasks.",
            ] if child_count > 0 else ["No child tasks."],
        },
    ]

    # Build the IR
    return {
        "schema_version": 2,
        "diagram_type": "workflow",
        "meta": {
            "title": f"Task {task_id[:8]}: {clip(task.title or 'Untitled', 40)}",
            "subtitle": f"Status: {task.status}, Step: {_step_label(task.workflow_step)}",
            "visual_preset": "blueprint",
            "animation": "none",
        },
        # A lane with nothing in it renders as an empty band that reads like a
        # loading state, so only lanes that carry a node are declared.
        "lanes": [
            lane for lane in (
                {"id": "flow", "label": "Workflow Steps"},
                {"id": "knowledge", "label": "Concepts"},
                {"id": "work", "label": "Children"},
            ) if any(n["lane"] == lane["id"] for n in nodes)
        ],
        "nodes": nodes,
        "edges": edges,
        "mainPath": main_path,
        "phases": phases,
        "cards": cards,
    }


def _is_step_completed(steps: list[dict], current_step: str | None, target_step_id: str) -> bool:
    """Check if target_step is before current_step in the workflow."""
    if not current_step:
        return False
    current_idx = next((i for i, s in enumerate(steps) if s["id"] == current_step), -1)
    target_idx = next((i for i, s in enumerate(steps) if s["id"] == target_step_id), -1)
    return target_idx < current_idx >= 0


def _step_label(step_id: str) -> str:
    """Convert step_id to human-readable label."""
    return step_id.replace("_", " ").title()
