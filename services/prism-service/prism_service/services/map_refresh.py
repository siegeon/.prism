"""Rebuild the Understand maps after a play lands.

ONE IMPLEMENTATION, TWO ENTRY POINTS — the shape `brain_health` already uses:
`api/workflows.py` `/steps/refresh-maps` (the drawn `refresh-maps` node) and
`ship_worker._refresh_maps_after_land` (the seat) both call `refresh_maps`,
so the node and the seat can never disagree about what the step does.

WHAT THIS STEP CAN AND CANNOT PROMISE. Each map is drawn from a PRISM store,
so a rebuild is only as fresh as the store under it:

  * `concepts` reads curated memory, which `brain_health.index_finished_play`
    re-indexes on this very trigger — so this map genuinely moves.
  * `language` reads the ontology, which a play may have added instances to.
  * `code` reads `graph.db`, and NOTHING on the land path rebuilds that graph
    (`ingest_source_to_brain` / `graph_svc.rebuild` run from the Re-sync
    action and `POST /api/graph/rebuild`, never from a ship). A code-map
    rebuild therefore redraws the graph AS IT CURRENTLY STANDS. That is not a
    false claim of freshness only because it is reported as such: every
    result carries `graph_stale`, and the history line says plainly when the
    code map was redrawn from a graph that is behind the source.

Never raises to its caller: a map is a reading aid, and a ship that already
succeeded is never failed by one.
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)

# The project-level maps on Understand. The `task` kind is deliberately NOT
# here: it is built for one task from the task page, not on every land.
PROJECT_MAP_KINDS: tuple[str, ...] = ("code", "concepts", "language")


def graph_is_stale(project: str) -> bool | None:
    """Whether graph.db is behind the source it is drawn from.

    Returns None when the answer cannot be determined — never a bare False,
    which a reader would take to mean "the graph is fresh".

    `api/staleness._derived_from_source_stale` is the only staleness signal
    PRISM has, and it answers a NARROWER question than this one: it compares
    a project's `last_analyzed_sha` against its pinned clone, and returns
    False outright for a project with no tracked remote. A project that
    ingests from the working tree — which is how this repo runs — therefore
    gets `False` meaning "the proxy does not apply here", not "the graph
    includes your landed commit". Reporting that as fresh would be the whole
    defect this function exists to avoid, so a project with no remote is
    UNKNOWN.
    """
    try:
        from prism_service.config import project_data_dir
        if not (project_data_dir(project) / "graph.db").exists():
            return True  # nothing has been built, so the map has no graph
    except Exception:  # noqa: BLE001
        return None

    try:
        from prism_service.engines import understand_engine as ue
        if not (ue._read_state(project).get("remote_url") or "").strip():
            return None  # the proxy cannot answer for a working-tree project
        from prism_service.api.staleness import _derived_from_source_stale
        return bool(_derived_from_source_stale(project))
    except Exception:  # noqa: BLE001 - an unknown answer is not "fresh"
        return None


def rebuild_graph(project: str) -> dict:
    """Recompute graph.db from the project's source.

    THE FIRST HALF OF THE STEP. Redrawing a map without this only redraws
    the graph as it already stood, so the picture would never contain the
    code that just shipped. Measured at ~8s on this repo (11,007 nodes /
    29,197 edges), which is why it belongs in the publish path rather than
    behind a button somebody has to remember.

    Returns {"ok", "nodes", "edges", "communities", "error"}. Never raises.
    """
    try:
        from prism_service.project_context import get_project
        ctx = get_project(project)
        result = ctx.graph_svc.rebuild(
            brain_db_path=str(ctx._data_dir / "brain.db"))
    except Exception as exc:  # noqa: BLE001 - a publish is never failed by this
        return {"ok": False, "nodes": 0, "edges": 0, "communities": 0,
                "error": str(exc)[:200]}
    # rebuild() reports a refusal in the payload without raising.
    error = str(result.get("error") or result.get("message") or "")
    return {
        "ok": not error,
        "nodes": int(result.get("nodes") or 0),
        "edges": int(result.get("edges") or 0),
        "communities": int(result.get("communities") or 0),
        "error": error[:200],
    }


def refresh_maps(
    project: str = "default",
    *,
    kinds: Iterable[str] | None = None,
    task_id: str | None = None,
    rebuild_the_graph: bool = True,
) -> dict:
    """Recompute the graph, redraw the maps, then diff each against the map
    it replaced. Best-effort throughout.

    THE ORDER IS THE POINT. Recompute first so the drawing describes the
    code that just shipped; diff last so a publish can say WHAT MOVED in the
    architecture, not merely that a new picture exists.

    Returns {"ok", "graph", "refreshed": [{kind, components, connections,
    changed}], "failed": [{kind, reason}], "graph_stale", "task_id"}.
    """
    wanted = tuple(kinds) if kinds is not None else PROJECT_MAP_KINDS
    refreshed: list[dict] = []
    failed: list[dict] = []

    graph = rebuild_graph(project) if rebuild_the_graph else {
        "ok": True, "nodes": 0, "edges": 0, "communities": 0,
        "error": "", "skipped": True}

    try:
        from prism_service.services.archify_service import ArchifyService
        svc = ArchifyService(project)
    except Exception as exc:  # noqa: BLE001 - the whole step is best-effort
        return {"ok": False, "refreshed": [], "graph": graph,
                "graph_stale": None, "task_id": task_id or "",
                "failed": [{"kind": k, "reason": f"archify unavailable: {exc}"}
                           for k in wanted]}

    for kind in wanted:
        # The map we are about to replace is the diff's base. Read it BEFORE
        # the rebuild overwrites ir.json, or there is nothing to compare to.
        previous = svc.ir(kind)
        try:
            meta = svc.build(kind)
        except Exception as exc:  # noqa: BLE001 - one bad map never stops the rest
            failed.append({"kind": kind, "reason": str(exc)[:200]})
            continue
        if not meta.get("ok"):
            failed.append({
                "kind": kind,
                "reason": str(meta.get("error") or "did not validate")[:200],
            })
            continue

        row = {
            "kind": kind,
            "components": meta.get("components", 0),
            "connections": meta.get("connections", 0),
            "changed": None,  # None = no previous map, so nothing to diff
        }
        if previous:
            diff = svc.compare(kind, previous)
            row["changed"] = diff["changed"] if diff.get("ok") else None
            if not diff.get("ok"):
                row["diff_error"] = diff.get("error", "")
        refreshed.append(row)

    return {
        "ok": not failed and graph.get("ok", False),
        "graph": graph,
        "refreshed": refreshed,
        "failed": failed,
        "graph_stale": graph_is_stale(project),
        "task_id": task_id or "",
    }


def _drawn(row: dict) -> str:
    """One map, with what MOVED in it when a diff was possible."""
    body = f"{row['kind']}({row['components']}c/{row['connections']}e"
    changed = row.get("changed")
    if changed is None:
        body += ", first draw" if "diff_error" not in row else ", not diffed"
    elif changed == 0:
        body += ", unchanged"
    else:
        body += f", {changed} changed"
    return body + ")"


def summarise(result: dict) -> str:
    """One line for the task's own history row.

    Says what was recomputed, what was drawn, and WHAT MOVED — a publish
    that changed nothing in the architecture reports "unchanged" rather
    than looking identical to one that was never measured.
    """
    graph = result.get("graph") or {}
    if graph.get("skipped"):
        head = "graph not recomputed"
    elif graph.get("ok"):
        head = (f"graph rebuilt ({graph.get('nodes', 0)} nodes/"
                f"{graph.get('edges', 0)} edges)")
    else:
        head = f"graph rebuild failed: {graph.get('error') or 'unknown'}"

    done = ", ".join(_drawn(r) for r in result.get("refreshed", [])) or "none"
    line = f"{head}; maps redrawn: {done}"
    for bad in result.get("failed", []):
        line += f"; {bad['kind']} failed: {bad['reason']}"
    # Only meaningful when the graph was NOT recomputed in this same pass.
    if graph.get("skipped"):
        stale = result.get("graph_stale")
        if stale is True:
            line += "; graph.db is behind the source"
        elif stale is None:
            line += "; graph freshness unknown"
    return line
