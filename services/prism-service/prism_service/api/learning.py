"""Learning API — Layer-A quality rows + variant performance rankings."""

from fastapi import APIRouter, Query

from prism_service.project_context import get_project
from prism_service.services.learning_data import (
    get_learning_rows,
    get_recent_reflections,
    get_variant_performance,
)

router = APIRouter()


@router.get("")
def overview(
    project: str = Query("default"),
    limit: int = Query(100, ge=1, le=500),
    reflections_limit: int = Query(25, ge=1, le=200),
) -> dict:
    ctx = get_project(project)
    scores_db = str(ctx._data_dir / "scores.db")
    return {
        "rows": get_learning_rows(scores_db, limit=limit),
        "variants": get_variant_performance(scores_db),
        # v6.0.18 — surface consolidation_runs directly so /learning is no
        # longer empty after a reflection runs (task_quality_rollup needs
        # a task_id; transcript-derived candidates only carry session_id).
        "recent_reflections": get_recent_reflections(scores_db, limit=reflections_limit),
    }
