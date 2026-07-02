"""Conductor API — prompt variants, scores, session outcomes, and SDLC state."""

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from prism_service.project_context import get_project
from prism_service.services.conductor_service import board_health

router = APIRouter()


def _svc(project: str):
    try:
        return get_project(project).conductor_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")


def _effective_project(query_project: Optional[str], body: dict) -> str:
    """Resolve the project for the mutation routes (task 48b965e0).

    Query param wins (the documented REST convention); a string
    body['project'] is the fallback for MCP-shaped clients that carry
    project in the JSON body (the tasks-API convention); 'default' last.
    Previously the body key was silently DROPPED and a real task came
    back as bare 'unknown task' from the wrong project."""
    if query_project:
        return query_project
    body_project = body.get("project")
    if isinstance(body_project, str) and body_project.strip():
        return body_project.strip()
    return "default"


def _name_searched_project(out: dict, task_id: str, project: str) -> dict:
    """Enrich the service's bare 'unknown task' refusal so the failure
    names WHICH project was searched (task 48b965e0 FR-4)."""
    if out.get("ok") is False and out.get("reason") == "unknown task":
        out["reason"] = f"task {task_id!r} not found in project {project!r}"
    return out


@router.get("/state")
def state(project: str = Query("default"), outcomes_limit: int = Query(200, ge=1, le=1000)) -> dict:
    s = _svc(project)
    return {
        "exploration_rate": s.exploration_rate(),
        "variants": s.get_variants(),
        "scores": s.get_scores(),
        "session_outcomes": s.get_session_outcomes(limit=outcomes_limit),
        "retired": s.get_retired(),
        # Conductor v2 (#79 follow-up): SPA /conductor page reads these to
        # render the SDLC dashboard — which tasks conductor is driving and
        # where they are in the workflow.
        "managed_tasks": s.managed_tasks(),
        "step_buckets": s.step_buckets(),
        # GoalBuddy GAP-5: cross-task "reorient" signal composed from the
        # per-task '⚠' advisory notes — fires when >= 2 low-value done-root
        # completions lead the newest-first run. Additive; SPA renders a badge.
        "board_health": board_health(_board_tasks(s)),
    }


def _board_tasks(s) -> list:
    """All tasks for the board_health scan (done-root filtering happens there)."""
    svc = getattr(s, "_task_svc", None)
    if svc is None:
        return []
    try:
        return svc.list()
    except Exception:
        return []


@router.post("/gate")
def gate(project: Optional[str] = Query(None), body: dict = Body(...)) -> dict:
    """Resolve a pending conductor gate from the SPA.

    Same path as the MCP `conductor_gate` tool: delegates to
    ConductorService.gate_decide. approve flips gate_state->'passed' and
    auto-advances; reject flips->'failed' and stores reason in
    task.gate_reason. The decision PERSISTS on the task row.
    """
    effective = _effective_project(project, body)
    s = _svc(effective)
    task_id = body.get("task_id")
    action = body.get("action")
    if not task_id or action not in ("approve", "reject"):
        raise HTTPException(422, "task_id and action ('approve'|'reject') required")
    out = s.gate_decide(
        task_id,
        action,
        reason=body.get("reason", "") or "",
        override=bool(body.get("override", False)),
        session_id=body.get("session_id"),
        # NO-SELF-OVERRIDE actor (task 3826dac3): defaults to the session.
        actor=body.get("actor") or body.get("session_id"),
        # HONEST RE-VERIFY (task 19e31e88): re-run the verifier fresh on a
        # latched-failed gate instead of stamping an override.
        re_verify=bool(body.get("re_verify", False)),
    )
    return _name_searched_project(out, task_id, effective)


@router.post("/advance")
def advance(project: Optional[str] = Query(None), body: dict = Body(...)) -> dict:
    """Advance a task to the next WORKFLOW_STEP (same as the MCP advance)."""
    effective = _effective_project(project, body)
    s = _svc(effective)
    task_id = body.get("task_id")
    if not task_id:
        raise HTTPException(422, "task_id required")
    out = s.advance_task(
        task_id,
        validation=body.get("validation"),
        session_id=body.get("session_id"),
    )
    # Normalize key for the SPA: advance_task returns 'to_step'.
    return _name_searched_project(out, task_id, effective)
