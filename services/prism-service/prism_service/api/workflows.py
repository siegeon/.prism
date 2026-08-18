"""Workflows API — the conductor FSM, its bots, and live occupancy as ONE
assembled read view.

In PRISM a workflow IS a bot: an FSM that agentically interacts with the
conductor's FSM. Both halves already exist, so this endpoint mints NOTHING
— no table, no persisted model, no parallel step definition. Every field is
read straight off an existing source of truth:

    steps      models/workflow.py WORKFLOW_STEPS  — the conductor FSM
    persona    models/roles.py STEP_ROLES         — who OWNS each step
    bots       services/context_builder.py ROLE_CARDS — the role briefs
    occupancy  the project's existing task rows   — task.workflow_step

`persona` is deliberately NOT a copy of the FSM row's `agent`. A gate has
agent=None because nobody AUTHORS a gate; the Steward ADJUDICATES it as the
independent reviewer (models/roles.py STEP_ROLES, enforced in
conductor_service.gate_decide). Resolving through role_for_step is what
lets the UI name that actor on a gate row.

This is also the single source of the step ORDERING for the SPA:
lib/workflowChips.ts used to carry a hand-maintained duplicate of
WORKFLOW_STEPS that nothing kept in sync. The rail now fetches it here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from prism_service.models import roles
from prism_service.models.workflow import WORKFLOW_STEPS
from prism_service.project_context import get_project
from prism_service.services.context_builder import ROLE_CARDS

router = APIRouter()

# The four cards an agent can wear on this board. sm/qa/dev are the canonical
# conductor roles; `architect` is a context_builder-only brief that folds to
# sm at routing time (models/roles.py ROLE_ALIASES) but is still a distinct
# hat worth drawing, so it is listed explicitly rather than derived.
BOT_IDS = ("sm", "qa", "dev", "architect")


def _persona_label(role_id: str) -> str:
    """Human label for a role id. Canonical roles carry their own label in
    the registry; `architect` has no Role row (it aliases to sm) so it falls
    back to its own name rather than being mislabelled "Steward"."""
    role = roles.ROLES.get(role_id)
    return role.label if role else role_id.capitalize()


def _occupancy(project: str, step_ids: list[str]) -> dict[str, int]:
    """How many tasks are standing at each step RIGHT NOW, per project.

    Keyed by the FSM's own steps only, and seeded to 0 so the renderer can
    read a count directly instead of branching on presence. A done task is
    not standing anywhere, and a legacy row parked at a step id the FSM no
    longer contains must not invent a node the canvas cannot draw.
    """
    try:
        svc = get_project(project).task_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")

    counts = {sid: 0 for sid in step_ids}
    for task in svc.list():
        if getattr(task, "status", "") == "done":
            continue
        step = getattr(task, "workflow_step", "") or ""
        if step in counts:
            counts[step] += 1
    return counts


@router.get("")
def get_workflows(project: str = Query("default")) -> dict:
    """The conductor FSM, the bots that drive it, and who is standing where."""
    steps = []
    for step in WORKFLOW_STEPS:
        persona = roles.role_for_step(step["id"])
        steps.append({
            "id": step["id"],
            "agent": step["agent"],
            "type": step["type"],
            "validation": step["validation"],
            "persona": persona,
            "persona_label": _persona_label(persona),
        })

    bots = [
        {"id": bid, "persona_label": _persona_label(bid), "card": ROLE_CARDS[bid]}
        for bid in BOT_IDS
    ]

    return {
        "steps": steps,
        "bots": bots,
        "occupancy": _occupancy(project, [s["id"] for s in steps]),
    }
