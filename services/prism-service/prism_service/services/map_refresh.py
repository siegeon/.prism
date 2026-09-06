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


def refresh_maps(
    project: str = "default",
    *,
    kinds: Iterable[str] | None = None,
    task_id: str | None = None,
) -> dict:
    """Rebuild the project-level Understand maps. Best-effort per map.

    Returns {"ok", "refreshed": [{kind, components, connections}],
             "failed": [{kind, reason}], "graph_stale": bool | None,
             "task_id"}. `ok` is True only when every requested map rebuilt.
    """
    wanted = tuple(kinds) if kinds is not None else PROJECT_MAP_KINDS
    refreshed: list[dict] = []
    failed: list[dict] = []

    try:
        from prism_service.services.archify_service import ArchifyService
        svc = ArchifyService(project)
    except Exception as exc:  # noqa: BLE001 - the whole step is best-effort
        return {"ok": False, "refreshed": [], "graph_stale": None,
                "task_id": task_id or "",
                "failed": [{"kind": k, "reason": f"archify unavailable: {exc}"}
                           for k in wanted]}

    for kind in wanted:
        try:
            meta = svc.build(kind)
        except Exception as exc:  # noqa: BLE001 - one bad map never stops the rest
            failed.append({"kind": kind, "reason": str(exc)[:200]})
            continue
        if meta.get("ok"):
            refreshed.append({
                "kind": kind,
                "components": meta.get("components", 0),
                "connections": meta.get("connections", 0),
            })
        else:
            failed.append({
                "kind": kind,
                "reason": str(meta.get("error") or "did not validate")[:200],
            })

    return {
        "ok": not failed,
        "refreshed": refreshed,
        "failed": failed,
        "graph_stale": graph_is_stale(project),
        "task_id": task_id or "",
    }


def summarise(result: dict) -> str:
    """One line for the task's own history row.

    Names what was drawn AND what the drawing is worth: a code map redrawn
    from a graph that is behind the source is reported as exactly that.
    """
    done = ", ".join(
        f"{r['kind']}({r['components']}c/{r['connections']}e)"
        for r in result.get("refreshed", [])
    ) or "none"
    line = f"maps rebuilt: {done}"
    for bad in result.get("failed", []):
        line += f"; {bad['kind']} failed: {bad['reason']}"
    stale = result.get("graph_stale")
    if stale is True:
        line += ("; graph.db is behind the source, so the code map redrew "
                 "the graph as it stands")
    elif stale is None:
        line += "; graph freshness unknown"
    return line
