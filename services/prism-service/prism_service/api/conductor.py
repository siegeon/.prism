"""Conductor API — prompt variants, scores, session outcomes, and SDLC state."""

from fastapi import APIRouter, HTTPException, Query

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
