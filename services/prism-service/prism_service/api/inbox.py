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

AC-6 ("everyone sees their own work", task 0784729f): unlike tasks.py this
is a PERSONAL surface, not a project data-access surface, so two people
hitting the identical URL see different things:
  - distinct-actor: a gate whose most recent producing actor (task_history)
    IS the caller never lands in THEIR OWN needs_you — you cannot sign your
    own work, so it is not yours to decide (owner rule).
  - membership: in team mode, a caller who is not a member of the project's
    workspace gets an EMPTY inbox — never a 403, never a peeked title. The
    query-string `project` stays a selector, never an ACL
    (test_workspace_authorization.py's rule); missing membership degrades
    this personal read to "nothing needs you", not an error.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from prism_service.api.auth import coerce_principal, current_principal
from prism_service.models.workflow import WORKFLOW_STEPS
from prism_service.models.workspace import Principal
from prism_service.project_context import get_project
from prism_service.services.workspace_service import get_workspace_service

router = APIRouter()

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


def _producer_actor(svc, task_id: str) -> str:
    """Best-effort identity of whoever produced the work now parked at this
    gate: the most recent non-blank task_history actor. Advisory — history
    actors are today mostly session ids / 'conductor', not a workspace user
    id or email, because no caller identity is threaded through
    advance/gate_decide yet — but it is the one signal that exists, and it
    is exactly right whenever a real Principal's id/email IS the recorded
    actor (e.g. a human-driven gate decision)."""
    for row in reversed(svc.history(task_id)):
        if row.actor:
            return row.actor
    return ""


def _is_producer(principal: Principal, actor: str) -> bool:
    if not actor:
        return False
    return actor == principal.user_id or actor.lower() == principal.email.lower()


def _member_of_projects_workspace(project: str, principal: Principal) -> bool:
    """Local mode is single-user by definition — no workspace question.
    Team mode fails CLOSED: no binding and no membership row both read as
    'not a member'; never raise here, the caller just gets an empty inbox
    (module docstring)."""
    if principal.mode != "team":
        return True
    ws_service = get_workspace_service()
    workspace = ws_service.project_workspace(project)
    if workspace is None:
        return False
    return ws_service.membership_for(workspace.id, principal.user_id) is not None


@router.get("")
def get_inbox(
    project: str = Query("default"),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal = coerce_principal(principal)
    viewer = {
        "user_id": principal.user_id,
        "email": principal.email,
        "display_name": principal.display_name,
    }
    if not _member_of_projects_workspace(project, principal):
        return {"needs_you": [], "activity": [], "viewer": viewer}

    svc = _svc(project)
    tasks = svc.list()

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

        # ROOT TASKS ONLY in "needs you". A subtask parked at a gate looks
        # identical to a root parked at a gate, and surfacing it would put
        # the driver's own decomposition in front of the owner — owner rule
        # 2026-08-06: subtasks are "beneath the notice of the human" and are
        # resolved by whoever drives them, never handed back. Activity below
        # is unaffected: seeing that a child is mid-step is information, not
        # a demand for action.
        if gate_state == "pending" and not parent_id:
            if _is_producer(principal, _producer_actor(svc, task_id)):
                # distinct-actor: you produced this work, so it is not
                # yours to decide — it stays off YOUR inbox even though the
                # gate is genuinely pending (a distinct member's inbox
                # still carries it).
                continue
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

    return {"needs_you": needs_you, "activity": activity, "viewer": viewer}
