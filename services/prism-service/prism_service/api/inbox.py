"""Inbox — a read projection of conductor state, never a second store.

Approved prototype (task 0784729f, "Inbox" screen): "Every message is
conductor state, projected: a message never mints a task." So this router
is GET-only and derives everything from the same task rows the board and
the task detail page already read — no new table, no write path.

Two groups, matching the mock's "# my-work" stream:
  needs_you — tasks parked at a pending gate (the agent that produced the
              work cannot sign its own gate; a human addressee can).
  activity  — tasks the conductor is actively driving (a real workflow_step,
              not stuck at a gate) — the "verifier-01: working, step 6 of 7"
              line.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from prism_service.api.auth import authorize_project_request
from prism_service.models.workflow import WORKFLOW_STEPS
from prism_service.project_context import get_project

router = APIRouter(dependencies=[Depends(authorize_project_request)])

# A task in any of these statuses is finished, one way or another — it never
# belongs in an Inbox meant to empty.
_TERMINAL_STATUSES = {"done", "cancelled", "deleted", "archived"}

_STEP_INDEX = {step["id"]: i for i, step in enumerate(WORKFLOW_STEPS)}
_STEP_COUNT = len(WORKFLOW_STEPS)


def _svc(project: str):
    return get_project(project).task_svc


def _step_position(step_id: str) -> str:
    """'step 6 of 10' for a known WORKFLOW_STEPS id, else ''."""
    idx = _STEP_INDEX.get(step_id)
    if idx is None:
        return ""
    return f"step {idx + 1} of {_STEP_COUNT}"


@router.get("")
def get_inbox(project: str = Query("default")) -> dict:
    tasks = _svc(project).list()

    needs_you: list[dict] = []
    activity: list[dict] = []

    for t in tasks:
        status = str(getattr(t, "status", "") or "")
        if status in _TERMINAL_STATUSES:
            continue

        workflow_step = str(getattr(t, "workflow_step", "") or "")
        gate_state = str(getattr(t, "gate_state", "") or "")
        task_id = getattr(t, "id", "")
        title = getattr(t, "title", "")
        parent_id = getattr(t, "parent_id", "") or ""
        url = f"/tasks/{task_id}"

        if gate_state == "pending":
            needs_you.append({
                "task_id": task_id,
                "title": title,
                "workflow_step": workflow_step,
                "gate_reason": getattr(t, "gate_reason", "") or "",
                "parent_id": parent_id,
                "url": url,
            })
        elif workflow_step:
            activity.append({
                "task_id": task_id,
                "title": title,
                "workflow_step": workflow_step,
                "step_position": _step_position(workflow_step),
                "parent_id": parent_id,
                "url": url,
            })

    return {"needs_you": needs_you, "activity": activity}
