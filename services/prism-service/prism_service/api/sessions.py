"""Sessions API — recent session outcomes and skill usage."""

from fastapi import APIRouter, HTTPException, Query

from prism_service.project_context import get_project
from prism_service.services import claude_transcripts as ct

router = APIRouter()


def _svc(project: str):
    try:
        return get_project(project).conductor_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")


@router.get("")
def overview(project: str = Query("default"), limit: int = Query(50, ge=1, le=500)) -> dict:
    s = _svc(project)
    return {
        "outcomes": s.get_session_outcomes(limit=limit),
        "skill_usage": s.get_skill_usage(),
    }


@router.post("/import-transcripts")
def import_transcripts(project: str = Query("default")) -> dict:
    """Manual trigger for the disk-reader: parse every ~/.claude/projects/
    transcript for this project, populate session_outcomes with any
    session not yet imported. The background timer does this every 60s;
    this endpoint exists so the SPA can offer a one-click backfill."""
    try:
        ctx = get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    from prism_service.engines import understand_engine as ue
    state = ue._read_state(project)
    sp = (state.get("source_path") or "").strip()
    if not sp:
        return {"imported": 0, "skipped_reason": "project has no source_path"}
    scores_db = str(ctx._data_dir / "scores.db")
    n = ct.import_unseen(scores_db, sp)
    return {"imported": n, "source_path": sp}
