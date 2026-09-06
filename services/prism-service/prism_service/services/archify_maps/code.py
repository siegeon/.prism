"""Archify map builder: the code map."""

from __future__ import annotations

from prism_service.project_context import get_project
from prism_service.services.archify_maps._layout import slug, clip, place_grid

DIAGRAM_TYPE = "architecture"

# Type inference keywords
_TYPE_KEYWORDS = {
    "frontend": {"web", "ui", "tsx", "react", "spa", "page", "component", "view"},
    "backend": {"api", "server", "service", "route", "handler", "worker", "task", "engine", "core"},
    "database": {"db", "store", "storage", "sqlite", "graph", "memory", "cache", "redis", "sql"},
    "messagebus": {"queue", "event", "pubsub", "kafka", "stream", "broker", "bus", "mcp", "webhook"},
    "security": {"auth", "policy", "gate", "token", "permission", "access", "rule", "governance"},
}


def _infer_type(label: str, top_files: list[str]) -> str:
    """Infer component type from community label and top files."""
    text = (label + " " + " ".join(top_files)).lower()

    for ctype, keywords in _TYPE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return ctype
    return "backend"


def build(project: str, *, task_id: str | None = None) -> dict:
    """Build the code architecture map from graph communities and edges."""
    try:
        ctx = get_project(project)
        graph_svc = ctx.graph_svc
        communities = graph_svc.communities()
    except Exception:
        # Empty diagram when graph_svc fails
        return {
            "schema_version": 1,
            "diagram_type": "architecture",
            "meta": {
                "title": "Code architecture",
                "subtitle": "graph.db has no communities. Run POST /api/graph/rebuild.",
                "visual_preset": "blueprint",
                "animation": "none",
            },
            "layout": {"mode": "grid", "cols": 2, "cellW": 170, "cellH": 76, "gapX": 28, "gapY": 34},
            "components": [{"id": "empty", "type": "external", "label": "No data yet", "row": 0, "col": 0}],
            "cards": [
                {
                    "dot": "slate",
                    "title": "Graph empty",
                    "items": ["graph.db has no communities. Run POST /api/graph/rebuild."],
                }
            ],
        }

    if not communities:
        return {
            "schema_version": 1,
            "diagram_type": "architecture",
            "meta": {
                "title": "Code architecture",
                "subtitle": "No communities found.",
                "visual_preset": "blueprint",
                "animation": "none",
            },
            "layout": {"mode": "grid", "cols": 2, "cellW": 170, "cellH": 76, "gapX": 28, "gapY": 34},
            "components": [{"id": "empty", "type": "external", "label": "No data yet", "row": 0, "col": 0}],
            "cards": [
                {
                    "dot": "slate",
                    "title": "No communities",
                    "items": ["No code communities detected in graph.db."],
                }
            ],
        }

    # Keep top N communities by size (up to 12)
    max_components = 12
    kept_communities = communities[:max_components]
    total_communities = len(communities)
    total_entities = sum(c["size"] for c in communities)

    # Build components
    components = []
    community_by_id = {}

    for comm in kept_communities:
        cid = slug(f"c{comm['id']}-{comm['label']}")
        ctype = _infer_type(comm["label"], comm.get("top_files", []))
        label = clip(comm["label"], 16)
        sublabel = f"{comm['size']}"

        # Tag: top entity name if short
        tag = None
        if comm.get("top_entities"):
            top_ent = comm["top_entities"][0]
            if isinstance(top_ent, str) and len(top_ent) <= 20:
                tag = top_ent

        comp = {
            "id": cid,
            "type": ctype,
            "label": label,
            "sublabel": sublabel,
        }
        if tag:
            comp["tag"] = tag
        components.append(comp)
        community_by_id[comm["id"]] = (cid, comm, ctype)

    # Which community owns each file, across EVERY kept community. The edge
    # query must run ONCE over the whole file set: asking it for one
    # community's files at a time can only ever return that community's
    # internal edges, so every cross-community edge was discarded and the map
    # came out with no connections at all.
    file_owner: dict[str, str] = {}
    all_files: list[str] = []
    for comm_id, (cid, _comm, _t) in community_by_id.items():
        try:
            files = graph_svc.community_files(comm_id)[:200]
        except Exception:
            continue
        for f in files:
            if f not in file_owner:
                file_owner[f] = cid
                all_files.append(f)

    edges_map: dict[tuple[str, str], int] = {}
    try:
        for edge in graph_svc.edges_between_files(all_files):
            src = file_owner.get(edge["from"])
            tgt = file_owner.get(edge["to"])
            if not src or not tgt or src == tgt:
                continue
            key = (src, tgt)
            edges_map[key] = edges_map.get(key, 0) + int(edge.get("weight", 1))
    except Exception:
        edges_map = {}

    # Place components in a simple grid
    comp_ids = [c["id"] for c in components]
    placements = place_grid([comp_ids], cols=3)

    # Draw the heaviest dependencies, but only between GRID-ADJACENT
    # components. Archify refuses a route that passes through an unrelated
    # component, and a generated map cannot hand-route waypoints the way the
    # authored examples do — so a long edge across the grid is not a stronger
    # map, it is an invalid one. Adjacent pairs give short segments that
    # always route cleanly, and the heaviest seam a pair carries is named in
    # the card list instead of on the line.
    def _adjacent(a: str, b: str) -> bool:
        if a not in placements or b not in placements:
            return False
        (r1, c1), (r2, c2) = placements[a], placements[b]
        return abs(r1 - r2) + abs(c1 - c2) == 1

    ranked = sorted(edges_map.items(), key=lambda kv: -kv[1])
    connections = []
    drawn: set[frozenset] = set()
    for (from_id, to_id), weight in ranked:
        pair = frozenset((from_id, to_id))
        if pair in drawn or not _adjacent(from_id, to_id):
            continue
        drawn.add(pair)
        conn = {"from": from_id, "to": to_id}
        if len(connections) < 3:
            conn["variant"] = "emphasis"
        connections.append(conn)
        if len(connections) >= 14:
            break

    # The seams worth naming, whether or not the grid let us draw them.
    heaviest = [
        f"{a.split('-', 1)[-1][:22]} → {b.split('-', 1)[-1][:22]} ({w})"
        for (a, b), w in ranked[:5]
    ]

    # Apply placements
    for comp in components:
        if comp["id"] in placements:
            row, col = placements[comp["id"]]
            comp["row"] = row
            comp["col"] = col

    # Create boundaries (disabled due to routing constraints)
    boundaries = []

    # Create views
    views = [
        {
            "id": "top-communities",
            "label": "Top communities",
            "focus": [c["id"] for c in components],
            "note": f"{len(components)} largest code communities",
        }
    ]

    # Create cards
    total_connections = len(connections)
    cards = [
        {
            "dot": "cyan",
            "title": "Structure",
            "items": [
                f"{len(kept_communities)} of {total_communities} communities mapped.",
                f"{total_entities} total entities across all code.",
            ],
        },
        {
            "dot": "emerald",
            "title": "Heaviest dependencies",
            "items": heaviest or [f"{total_connections} file dependencies."],
        },
        {
            "dot": "slate",
            "title": "Data source",
            "items": ["graph.db, rebuilt by POST /api/graph/rebuild."],
        },
    ]

    return {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": {
            "title": "Code architecture",
            "subtitle": f"{len(kept_communities)} communities, {total_connections} relationships",
            "visual_preset": "blueprint",
            "animation": "none",
            "views": views[:5],
        },
        "layout": {
            "mode": "grid",
            "cols": 3,
            "cellW": 220,
            "cellH": 100,
            "gapX": 20,
            "gapY": 20,
        },
        "components": components,
        "connections": connections,
        "boundaries": boundaries,
        "cards": cards,
    }
