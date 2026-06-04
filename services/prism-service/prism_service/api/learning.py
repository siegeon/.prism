"""Learning API — Layer-A quality rows + variant performance rankings."""

from fastapi import APIRouter, Body, HTTPException, Query

from prism_service.project_context import get_project
from prism_service.services.adaptive_policy import (
    DEFAULT_KNOBS,
    KNOB_BOUNDS,
    AdaptivePolicyService,
    _clamp,
    get_active_knobs,
)
from prism_service.services.agent_runs_data import get_agent_run_aggregates
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
        # task f4498190 — agent-run telemetry aggregates feed the /learning
        # agent-timeline panel (avg duration/step, override rate, token/role).
        "agent_runs": get_agent_run_aggregates(scores_db),
    }


@router.get("/policy")
def policy(project: str = Query("default")) -> dict:
    """Tier-3 adaptive policy — current tuned knobs + tuning history + the
    per-op verdict accuracy table. Backs the /learning 'Adaptive policy' panel.
    """
    ctx = get_project(project)
    scores_db = str(ctx._data_dir / "scores.db")
    svc = AdaptivePolicyService(scores_db, mem=getattr(ctx, "memory_svc", None))
    return {
        "knobs": get_active_knobs(scores_db),
        "history": svc.history(),
        "op_accuracy": svc.op_verdict_accuracy(),
    }


@router.post("/policy")
def set_policy(project: str = Query("default"), body: dict = Body(...)) -> dict:
    """Manually SET or REVERT the Tier-3 adaptive knobs from the SPA.

    body.action=='revert' restores DEFAULT_KNOBS. Otherwise body carries one
    or more knob values (forget_cutoff/decay_weight/merge_similarity_threshold)
    that are clamped to KNOB_BOUNDS and persisted as a new policy_knobs row —
    get_active_knobs reads the latest, so the change takes effect immediately.
    """
    ctx = get_project(project)
    scores_db = str(ctx._data_dir / "scores.db")
    svc = AdaptivePolicyService(scores_db, mem=getattr(ctx, "memory_svc", None))
    action = (body.get("action") or "set").strip()

    if action == "revert":
        svc._persist(dict(DEFAULT_KNOBS), rationale="manual revert to defaults (SPA)")
        return {"ok": True, "action": "revert", "knobs": get_active_knobs(scores_db)}

    current = dict(get_active_knobs(scores_db))
    touched = False
    for k in KNOB_BOUNDS:
        if k in body and body[k] is not None:
            try:
                current[k] = _clamp(k, float(body[k]))
                touched = True
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
    if not touched:
        raise HTTPException(422, "no knob values supplied")
    svc._persist(current, rationale="manual set (SPA)")
    return {"ok": True, "action": "set", "knobs": get_active_knobs(scores_db)}
