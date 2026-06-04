"""Per-project staleness signal for the sidebar dot indicators.

Returns booleans for the few domains that have a meaningful "stale vs
current" notion against the project's source SHA. Cheap by design — the
sidebar polls this every few seconds.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from prism_service.engines import understand_engine as ue
from prism_service.project_context import get_project
from prism_service.services import source_service as ss

router = APIRouter()


def _understand_stale(project: str) -> bool:
    state = ue._read_state(project)
    remote = (state.get("remote_url") or "").strip()
    if not remote:
        return False  # sourceless projects can't be stale
    if not ss.is_cloned(project):
        return True
    try:
        head = ss.current_sha(project)
    except ss.SourceUnavailable:
        return True
    last = state.get("last_analyzed_sha")
    return last != head


def _derived_from_source_stale(project: str) -> bool:
    """True when this project tracks a git source AND any source-derived
    index hasn't been (re)built against the current pinned SHA.

    Today there's no graph_from_source / brain_from_source flow — both
    layers ingest from the user's working tree, not the project's pinned
    clone (gap to close in v5.2). So we treat the project's
    `last_analyzed_sha` as the proxy "have we processed this SHA at all"
    signal: if that hasn't caught up, every source-derived domain is
    stale, by definition. Once graph/brain learn to ingest from the
    source clone, they each get their own last_built_sha column.
    """
    state = ue._read_state(project)
    remote = (state.get("remote_url") or "").strip()
    if not remote:
        return False
    if not ss.is_cloned(project):
        return True
    try:
        head = ss.current_sha(project)
    except ss.SourceUnavailable:
        return True
    return state.get("last_analyzed_sha") != head


def _graph_legacy_stale(project: str) -> bool:
    """Pre-v5.1 docs-vs-disk drift signal — kept as an OR so projects that
    haven't been source-configured still flag local-drift issues."""
    try:
        ctx = get_project(project)
        gs = getattr(ctx, "graph_svc", None)
        if gs is None:
            return False
        brain_db = getattr(ctx.brain_svc, "db_path", None)
        if not brain_db:
            return False
        out = gs.sync_status(str(brain_db))
        return bool(out.get("stale"))
    except Exception:
        return False


@router.get("")
def staleness(project: str = Query("default")) -> dict:
    src_stale = _derived_from_source_stale(project)
    legacy = _graph_legacy_stale(project)
    return {
        "project": project,
        "understand": _understand_stale(project),
        "graph": src_stale or legacy,
        "brain": src_stale or legacy,
    }


@router.post("/resync")
def resync(project: str = Query("default")) -> dict:
    """Re-sync the source-derived indexes and clear the drift signal.

    Rebuilds the graph for ``project`` (the same path as POST /api/graph/rebuild)
    and advances the project's ``last_analyzed_sha`` to the current source HEAD
    so the graph/brain staleness flags clear. Backs the SPA drift card's
    'Re-sync' action — previously api/staleness.py had no write surface.
    """
    try:
        ctx = get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    out: dict = {"project": project}
    try:
        out["rebuild"] = ctx.graph_svc.rebuild()
    except Exception as exc:
        out["rebuild_error"] = str(exc)
    # Advance the analyzed SHA so the source-derived staleness flags clear.
    try:
        state = ue._read_state(project)
        if (state.get("remote_url") or "").strip() and ss.is_cloned(project):
            state["last_analyzed_sha"] = ss.current_sha(project)
            ue._write_state(project, state)
            out["last_analyzed_sha"] = state["last_analyzed_sha"]
    except Exception as exc:
        out["sha_error"] = str(exc)
    out["staleness"] = staleness(project)
    return out
