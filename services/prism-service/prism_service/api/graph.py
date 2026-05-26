"""Graph API — entity/relationship/community counts and rebuild trigger.

The interactive Sigma viewer is served from /graph/viewer/{project} (existing
NiceGUI route, preserved across cutover). This endpoint exposes the
summary stats and graph.json existence so the SPA can decide whether
to embed the viewer or show a 'rebuild' prompt.
"""

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from prism_service.config import project_data_dir
from prism_service.project_context import get_project

router = APIRouter()


def _count(db: Path, sql: str) -> int:
    if not db.exists():
        return 0
    try:
        c = sqlite3.connect(str(db)); v = c.execute(sql).fetchone(); c.close()
        return int(v[0]) if v else 0
    except Exception:
        return 0


@router.get("/summary")
def summary(project: str = Query("default")) -> dict:
    # v5.3.11 — use project_data_dir() so this resolves to the right
    # location on native installs (%LOCALAPPDATA%\prism\... or ~/.prism)
    # as well as docker (/data). Previously hardcoded /data/projects/...,
    # so on native installs every count came back 0 even with a populated
    # graph.db on disk.
    root = project_data_dir(project)
    graph_json = root / "graphify-src" / "graphify-out" / "graph.json"
    return {
        "entities": _count(root / "graph.db", "SELECT COUNT(*) FROM entities"),
        "relationships": _count(root / "graph.db", "SELECT COUNT(*) FROM relationships"),
        "communities": _count(root / "graph.db", "SELECT COUNT(DISTINCT community) FROM entities WHERE community IS NOT NULL"),
        "graph_json_exists": graph_json.exists(),
        "viewer_url": f"/graph/viewer/{project}",
    }


@router.post("/rebuild")
def rebuild(project: str = Query("default")) -> dict:
    # Pass brain_db_path so rebuild() can auto-backfill the graphify
    # staging dir from the docs table when it's empty. Without this, any
    # project whose source files were ingested via Brain but never
    # manually staged (i.e. virtually all of them) saw the Rebuild button
    # silently no-op: rebuild() returned {"message": "no staged source
    # files yet"}, the API discarded it, and graph.json was never produced.
    try:
        ctx = get_project(project)
        result = ctx.graph_svc.rebuild(
            brain_db_path=str(ctx._data_dir / "brain.db"),
        )
    except Exception as exc:
        raise HTTPException(500, f"rebuild failed: {exc}")
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(500, f"rebuild failed: {result['error']}")
    return {"ok": True, **(result if isinstance(result, dict) else {})}


class EdgesBetweenBody(BaseModel):
    paths: list[str]


@router.get("/communities")
def communities(project: str = Query("default")) -> dict:
    """List communities for the project graph."""
    try:
        svc = get_project(project).graph_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    return {"communities": svc.communities()}


@router.get("/community-files")
def community_files(
    community_id: int = Query(..., ge=0),
    project: str = Query("default"),
) -> dict:
    """Files in a single community."""
    try:
        svc = get_project(project).graph_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    return {"files": svc.community_files(community_id)}


@router.get("/file-detail")
def file_detail(
    path: str = Query(..., min_length=1),
    project: str = Query("default"),
) -> dict:
    """Per-file detail (entities + in/out edges) for the drill-down's
    node-level panel."""
    try:
        svc = get_project(project).graph_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    return svc.file_detail(path)


@router.post("/edges-between")
def edges_between(
    body: EdgesBetweenBody,
    project: str = Query("default"),
) -> dict:
    """File-to-file edges (weighted) for the given fileset.

    Powers the inside-a-layer drill view in /understand. Returns an
    empty list when graph.db hasn't been built for this project yet
    (call POST /api/graph/rebuild first, or push files via the Brain
    refresh path).
    """
    try:
        svc = get_project(project).graph_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    edges = svc.edges_between_files(body.paths)
    return {"edges": edges, "count": len(edges)}
