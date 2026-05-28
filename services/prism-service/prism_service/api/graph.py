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
def rebuild(
    project: str = Query("default"),
    reingest: bool = Query(False),
) -> dict:
    """Rebuild the graph for `project`.

    v6.0.19 — two changes versus the silent v5.x version:
      * Returns the `graph_svc.rebuild()` summary (nodes / edges /
        communities / imported_* / error / message) instead of swallowing
        it under a fixed `{"ok": True}`. The SPA was reporting success
        on every click even when graphify produced no graph.json
        because staging was full of venv site-packages.
      * `reingest=true` re-walks the project's source dir and re-stages
        every code file from disk first — the cheapest way to recover
        from a poisoned Brain after a skip-list fix landed.
    """
    try:
        ctx = get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    out: dict = {}
    if reingest:
        from prism_service.services.source_service import ingest_source_to_brain
        # Wipe staging before re-walking so the prior skip-list miss
        # doesn't leave stale .venvs/ files sitting in graphify-src/.
        try:
            ctx.graph_svc.clear_staging()
        except Exception:
            pass
        out["reingest"] = ingest_source_to_brain(project)
    try:
        ctx_brain_db = str(ctx._data_dir / "brain.db")
        result = ctx.graph_svc.rebuild(brain_db_path=ctx_brain_db)
    except Exception as exc:
        raise HTTPException(500, f"rebuild failed: {exc}")
    # The synchronous rebuild may set `error` or `message` without raising
    # — surface that to the caller so the SPA can show why no graph.json
    # appeared instead of pretending the click worked.
    out.update(result)
    out["ok"] = "error" not in result and "message" not in result
    return out


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


@router.get("/central")
def central(
    project: str = Query("default"),
    limit: int = Query(20, ge=1, le=200),
) -> dict:
    """Top-N entities by PageRank centrality (v6.1.5).

    Returns ``{entities: [{name, kind, file, line, community, centrality}]}``
    sorted by centrality desc. The score is recomputed each graph_rebuild
    and persisted on entities.centrality; rows with centrality == 0 are
    excluded (pre-v6.1.5 builds had no column).
    """
    try:
        svc = get_project(project).graph_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    return {"entities": svc.top_central_entities(limit=limit)}


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


@router.post("/enrich")
def enrich(
    project: str = Query("default"),
    max_scopes: int = Query(6, ge=1, le=40),
) -> dict:
    """Run one cluster-enrichment pass NOW (Ultimate Graph narrative layer,
    #50). Names changed hierarchy scopes via inference; escapes scopes whose
    files are unchanged. The background worker does this on a timer too —
    this is the manual on-demand trigger."""
    from prism_service.services import graph_enrich
    try:
        get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    return graph_enrich.enrich_project_once(project, max_scopes)


@router.get("/enrich/status")
def enrich_status(project: str = Query("default")) -> dict:
    """How many hierarchy scopes are named vs still pending (changed)."""
    from prism_service.services import graph_enrich
    try:
        graph = get_project(project).graph_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    scopes = graph_enrich.hierarchy_scopes(project)
    named = graph.annotations_for("hierarchy", "name")

    def _pending(s) -> bool:
        ann = graph.get_annotation("hierarchy", s["scope_id"], "name")
        return ann is None or ann.get("input_hash") != s["input_hash"]

    return {
        "scopes": len(scopes),
        "named": len([k for k, v in named.items() if v.get("name")]),
        "pending": sum(1 for s in scopes if _pending(s)),
        "worker_enabled": graph_enrich.is_enabled(),
    }


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
