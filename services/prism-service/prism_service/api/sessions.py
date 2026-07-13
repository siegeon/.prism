"""Sessions API — recent session outcomes and skill usage."""

import json

from fastapi import APIRouter, HTTPException, Query

from prism_service.project_context import get_project
from prism_service.services import claude_transcripts as ct
from prism_service.services import sqlite_db

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


def _json_paths(raw) -> list:
    """Decode a stored JSON-array path column to a list; [] for legacy rows
    (NULL/missing/malformed) so a pre-paths session never 500s the detail."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []


@router.get("/{session_id}")
def detail(session_id: str, project: str = Query("default")) -> dict:
    """One session's outcome row + the file paths it touched. Backs the SPA's
    /sessions/{id} detail page. Returns the session_outcomes scalar fields plus
    files_read_paths / files_modified_paths as JSON arrays ([] for legacy rows
    imported before path capture). 404s on an unknown id."""
    try:
        ctx = get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    scores_db = str(ctx._data_dir / "scores.db")
    try:
        conn = sqlite_db.connect(scores_db)
    except Exception as exc:
        raise HTTPException(500, f"scores.db unavailable: {exc}")
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(session_outcomes)").fetchall()}
        row = conn.execute(
            "SELECT * FROM session_outcomes WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, f"unknown session: {session_id}")
    d = dict(row)
    session = {k: v for k, v in d.items()
               if k not in ("files_read_paths", "files_modified_paths")}
    session["session_id"] = d.get("session_id")
    session["files_read_paths"] = (
        _json_paths(d.get("files_read_paths")) if "files_read_paths" in cols else []
    )
    session["files_modified_paths"] = (
        _json_paths(d.get("files_modified_paths")) if "files_modified_paths" in cols else []
    )
    return {"session": session}


@router.post("/import-transcripts")
def import_transcripts(project: str = Query("default"),
                       refresh_paths: bool = Query(False)) -> dict:
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
    n = ct.import_unseen(scores_db, sp, refresh_paths=refresh_paths)
    return {"imported": n, "source_path": sp,
            "refresh_paths": refresh_paths}
