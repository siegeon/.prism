"""Conductor API — prompt variants, scores, session outcomes, and SDLC state."""

from fastapi import APIRouter, Body, HTTPException, Query

from prism_service.project_context import get_project

router = APIRouter()


def _svc(project: str):
    try:
        return get_project(project).conductor_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")


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
    }


@router.post("/gate")
def gate(project: str = Query("default"), body: dict = Body(...)) -> dict:
    """Resolve a pending conductor gate from the SPA.

    Same path as the MCP `conductor_gate` tool: delegates to
    ConductorService.gate_decide. approve flips gate_state->'passed' and
    auto-advances; reject flips->'failed' and stores reason in
    task.gate_reason. The decision PERSISTS on the task row.
    """
    s = _svc(project)
    task_id = body.get("task_id")
    action = body.get("action")
    if not task_id or action not in ("approve", "reject"):
        raise HTTPException(422, "task_id and action ('approve'|'reject') required")
    return s.gate_decide(
        task_id,
        action,
        reason=body.get("reason", "") or "",
        override=bool(body.get("override", False)),
        session_id=body.get("session_id"),
    )


@router.post("/advance")
def advance(project: str = Query("default"), body: dict = Body(...)) -> dict:
    """Advance a task to the next WORKFLOW_STEP (same as the MCP advance)."""
    s = _svc(project)
    task_id = body.get("task_id")
    if not task_id:
        raise HTTPException(422, "task_id required")
    out = s.advance_task(
        task_id,
        validation=body.get("validation"),
        session_id=body.get("session_id"),
    )
    # Normalize key for the SPA: advance_task returns 'to_step'.
    return out
