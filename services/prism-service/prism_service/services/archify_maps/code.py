"""Archify map builder: the code map, drawn from the CODE.

WHAT THIS REPLACED, and why. The previous builder took `graph_svc.
communities()` -- statistical clusters from community detection -- and made
each cluster a "component": the label was the text after the last separator
(`_distinctive`), the type was a keyword match against that label
(`_infer_type`: "web/ui/tsx" -> frontend), and the drawn connections were
whichever cluster pairs happened to land next to each other on the grid. So
the boxes read `brain`, `index`, `resolve`, `ident`, `okf host` -- clipped
cluster-label tails, not modules -- nothing carried a code location, and the
subtitle's relationship count described the grid, not the codebase.

Owner: "thats not even code archeture ... showing data that is not code",
"it seems you still did not fix the fact you are not looking at the code
correctly, and you aer munging the data", and the pointer to archify itself:
"its susposed to be a skill that is run agast the code bacse".

Archify's own contract agrees -- it compiles typed JSON that an agent
produces BY READING THE REPOSITORY; it does not cluster for you. So this
builder reads the repository's real shape out of graph.db: real directories
as modules, real file and entity counts, and real import/call edges
aggregated between them. Every component carries the real path it stands
for, so a click can land on the code. Nothing is inferred from a label.
"""

from __future__ import annotations

from prism_service.project_context import get_project
from prism_service.services.archify_maps._layout import slug, clip, place_grid

DIAGRAM_TYPE = "architecture"

MAX_COMPONENTS = 12

# Role by REAL directory name, not by keyword-matching a cluster label. A
# directory that matches nothing is "backend" and is not pretended otherwise.
_ROLE_BY_SEGMENT = {
    "web": "frontend",
    "pages": "frontend",
    "components": "frontend",
    "live": "frontend",
    "api": "backend",
    "routes": "backend",
    "services": "backend",
    "engines": "backend",
    "inference": "backend",
    "mcp": "messagebus",
    "models": "database",
    "store": "database",
    "db": "database",
    "assets": "external",
    "scripts": "external",
}

# Tests are real code but not runtime architecture, and by entity count they
# would be the three largest boxes on this repo's map (4760 entities in
# tests/unit alone), burying the system they test. Excluded, and SAID so on
# the map rather than silently dropped.
_TEST_MARKERS = ("/tests/", "/test/", "/__tests__/")


def _is_test(path: str) -> bool:
    p = "/" + path.replace("\\", "/").strip("/") + "/"
    return any(m in p for m in _TEST_MARKERS)


def _dir_of(path: str) -> str:
    p = path.replace("\\", "/")
    return p.rsplit("/", 1)[0] if "/" in p else "."


def _role_for(directory: str) -> str:
    segments = [s for s in directory.split("/") if s]
    for seg in reversed(segments):
        role = _ROLE_BY_SEGMENT.get(seg)
        if role:
            return role
    return "backend"


def _parent_of(directory: str, label: str) -> str:
    """The segment just above what the label already shows, e.g.
    `.../prism_service/services` labelled "services" -> "prism_service"."""
    segments = [s for s in directory.split("/") if s]
    shown = len([s for s in label.split("/") if s])
    return segments[-(shown + 1)] if len(segments) > shown else ""


def _labels_for(directories: list[str]) -> dict[str, str]:
    """Shortest tail of each real path that is still unique among the kept
    modules: `.../prism_service/services` becomes "services", and if another
    kept module also ended in "services" both grow a segment until they
    differ. Always a real path tail, never a clipped label."""
    labels: dict[str, str] = {}
    for d in directories:
        segments = [s for s in d.split("/") if s] or [d]
        chosen = d
        for depth in range(1, len(segments) + 1):
            candidate = "/".join(segments[-depth:])
            clash = any(
                other != d
                and "/".join([s for s in other.split("/") if s][-depth:]) == candidate
                for other in directories
            )
            if not clash:
                chosen = candidate
                break
        labels[d] = chosen
    return labels


def _empty(card_title: str, card_item: str) -> dict:
    return {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": {
            "title": "Code architecture",
            "animation": "none",
        },
        "layout": {"mode": "grid", "cols": 2, "cellW": 170, "cellH": 76,
                   "gapX": 28, "gapY": 34},
        "components": [{"id": "empty", "type": "external",
                        "label": "No data yet", "row": 0, "col": 0}],
        "cards": [{"dot": "slate", "title": card_title, "items": [card_item]}],
    }


# Chapter grouping: which curated chapter a module belongs to, read off its
# own role and real path tail -- never a fixed list of ids, so the grouping
# still makes sense against a different repository's real directories. Per
# viewer-runtime.md, the Named Chapter Rail, Chapter Delta Preview, Story
# Beat Navigator, Follow Camera, Director Strip, Story Horizon, and
# shareable story-moment links all derive from meta.views, so one
# all-inclusive chapter (the old shape) makes every one of them inert.
_CHAPTER_BY_TAIL = {
    "api": "entry", "routes": "entry", "mcp": "entry", "cli": "entry",
    "services": "core", "engines": "core", "conductor": "core",
    "workflows": "core",
    "models": "data", "store": "data", "db": "data", "memory_ops": "data",
    "inference": "data", "graph": "data",
}
_ENTRYPOINT_FILES = {"main.py", "app.py", "__main__.py", "server.py"}
# id -> (chapter label, short note). Order here is also the reading order of
# meta.views.
_CHAPTER_META = {
    "entry": ("Entry points", "Where callers reach the system: API, MCP, CLI."),
    "core": ("Conductor & services core", "The engine that drives tasks."),
    "data": ("Data & memory", "Where state and the graph persist."),
    "web": ("Web surface", "The browser-facing app."),
    "support": ("Support & tooling", "Scripts and assets, outside runtime."),
}


def _chapter_for(role: str, tail: str, top_file: str) -> str:
    """Which curated chapter a module belongs to. `tail` is the module's own
    shortest unique label segment (never the full path, so an unrelated
    outer directory of the same name cannot hijack the grouping); `top_file`
    is its largest real file, used only to spot a process entry point."""
    if role == "frontend":
        return "web"
    if role == "external":
        return "support"
    if role == "database":
        return "data"
    hit = _CHAPTER_BY_TAIL.get(tail)
    if hit:
        return hit
    if top_file.rsplit("/", 1)[-1] in _ENTRYPOINT_FILES:
        return "entry"
    return "core"


def _order_by_coupling(module_ids: list[str],
                       weights: dict[tuple[str, str], int]) -> list[str]:
    """Greedy seriation: start from the most-coupled module, then repeatedly
    take whichever remaining module is most strongly tied to the one just
    placed.

    Archify refuses a route that passes through an unrelated component, and a
    generated map cannot hand-route waypoints, so only grid-ADJACENT pairs can
    be drawn. The old builder accepted whatever the arbitrary order happened
    to make adjacent; this puts the strongest REAL dependencies next to each
    other, so the lines that do get drawn are the ones that matter.
    """
    def tie(a: str, b: str) -> int:
        return weights.get((a, b), 0) + weights.get((b, a), 0)

    remaining = list(module_ids)
    if not remaining:
        return []
    remaining.sort(key=lambda m: -sum(tie(m, o) for o in module_ids if o != m))
    ordered = [remaining.pop(0)]
    while remaining:
        last = ordered[-1]
        remaining.sort(key=lambda m: (-tie(last, m), module_ids.index(m)))
        ordered.append(remaining.pop(0))
    return ordered


def build(project: str, *, task_id: str | None = None) -> dict:
    """Build the code architecture map from the repository's real modules."""
    try:
        graph = get_project(project).graph_svc.file_graph()
    except Exception:
        return _empty("Graph unavailable",
                      "graph.db could not be read. Run POST /api/graph/rebuild.")

    files = graph.get("files") or []
    if not files:
        return _empty("Graph empty",
                      "No code indexed. Run POST /api/graph/rebuild.")

    # --- real modules: directories that actually exist ---------------------
    entities_by_dir: dict[str, int] = {}
    files_by_dir: dict[str, int] = {}
    # The biggest real FILE in each module. A click has to land on something
    # xref can resolve, and a directory is not: GET /api/xref/neighbors
    # answers kind:"unresolved" with no neighbours for one, which is the
    # dead-end click the owner hit ("when i click on one of the nodes it does
    # not take me to the skills at ale"). A file resolves to kind:"code".
    top_file_by_dir: dict[str, tuple[int, str]] = {}
    skipped_test_entities = 0
    for row in files:
        path = str(row.get("file") or "")
        if not path:
            continue
        count = int(row.get("entities") or 0)
        if _is_test(path):
            skipped_test_entities += count
            continue
        d = _dir_of(path)
        entities_by_dir[d] = entities_by_dir.get(d, 0) + count
        files_by_dir[d] = files_by_dir.get(d, 0) + 1
        if count > top_file_by_dir.get(d, (-1, ""))[0]:
            top_file_by_dir[d] = (count, path)

    if not entities_by_dir:
        return _empty("No runtime modules",
                      "graph.db holds only test files; nothing to draw.")

    ranked_dirs = sorted(entities_by_dir, key=lambda d: -entities_by_dir[d])
    kept = ranked_dirs[:MAX_COMPONENTS]
    kept_set = set(kept)
    dir_id = {d: slug(f"m-{d}") for d in kept}
    labels = _labels_for(kept)

    # --- real edges, aggregated to those modules ---------------------------
    module_weights: dict[tuple[str, str], int] = {}
    total_module_deps = 0
    for edge in graph.get("edges") or []:
        src, tgt = str(edge.get("from") or ""), str(edge.get("to") or "")
        if not src or not tgt or _is_test(src) or _is_test(tgt):
            continue
        a, b = _dir_of(src), _dir_of(tgt)
        if a == b or a not in kept_set or b not in kept_set:
            continue
        key = (dir_id[a], dir_id[b])
        if key not in module_weights:
            total_module_deps += 1
        module_weights[key] = module_weights.get(key, 0) + int(edge.get("weight") or 1)

    # --- layout: strongest real dependencies placed adjacent ---------------
    ordered = _order_by_coupling([dir_id[d] for d in kept], module_weights)
    placements = place_grid([ordered], cols=3)

    id_to_dir = {v: k for k, v in dir_id.items()}
    components = []
    for comp_id in ordered:
        d = id_to_dir[comp_id]
        comp = {
            "id": comp_id,
            "type": _role_for(d),
            "label": clip(labels[d], 22),
            "sublabel": f"{files_by_dir[d]} files · {entities_by_dir[d]}",
            # The PARENT segment, not the whole path: archify validates that a
            # tag fits its box at the 6px legible minimum, and a full path
            # ("services/prism-service/prism_serv…") needs ~123px in a 112px
            # box, which fails the build outright. The label is already a
            # unique real path tail and x_targets carries the full location,
            # so this only has to say which tree the module sits in.
            "tag": clip(_parent_of(d, labels[d]), 18),
        }
        if comp_id in placements:
            comp["row"], comp["col"] = placements[comp_id]
        components.append(comp)

    def _adjacent(a: str, b: str) -> bool:
        if a not in placements or b not in placements:
            return False
        (r1, c1), (r2, c2) = placements[a], placements[b]
        return abs(r1 - r2) + abs(c1 - c2) == 1

    ranked_edges = sorted(module_weights.items(), key=lambda kv: -kv[1])
    connections = []
    drawn: set[frozenset] = set()
    for (from_id, to_id), _weight in ranked_edges:
        pair = frozenset((from_id, to_id))
        if pair in drawn or not _adjacent(from_id, to_id):
            continue
        drawn.add(pair)
        # SKILL.md: relationship labels are semantic data, not decoration.
        # graph.db aggregates every relation kind (call, import, ...)
        # between two files without keeping which, so "uses" is the honest
        # claim -- and short enough to always clear archify's label-mask
        # width check. Archify's default midpoint lands the label ON the
        # source component's own box on this tight 3-col grid; nudge it
        # toward whichever row the connection actually opens INTO (down for
        # same-row and downward edges, up for upward edges), diagnosed via
        # `archify validate ... --json`'s own labelDy suggestions.
        r_from = placements.get(from_id, (0, 0))[0]
        r_to = placements.get(to_id, (0, 0))[0]
        label_dy = 24 if r_to >= r_from else -24
        conn = {"from": from_id, "to": to_id, "label": "uses", "labelDy": label_dy}
        if len(connections) < 3:
            conn["variant"] = "emphasis"
        connections.append(conn)
        if len(connections) >= 14:
            break

    heaviest = [
        f"{labels[id_to_dir[a]]} → {labels[id_to_dir[b]]} ({w})"
        for (a, b), w in ranked_edges[:5]
    ]

    total_runtime_entities = sum(entities_by_dir.values())
    cards = [
        {
            "dot": "cyan",
            "title": "Modules",
            "items": [
                f"{len(kept)} of {len(entities_by_dir)} real directories drawn.",
                f"{total_runtime_entities} entities across runtime code.",
            ],
        },
        {
            "dot": "emerald",
            "title": "Heaviest dependencies",
            "items": heaviest or ["No dependencies between the drawn modules."],
        },
        {
            # THE COUNT MUST NOT FLATTER THE PICTURE. Only grid-adjacent pairs
            # can be routed, so the number of lines is a property of the
            # layout; the number of dependencies is a property of the code.
            # Saying only the first is how "13 relationships" came to describe
            # a codebase with thousands.
            "dot": "amber",
            "title": "What is drawn",
            "items": [
                f"{len(connections)} of {total_module_deps} module dependencies drawn.",
                "Only neighbouring modules can be routed; the rest are listed above.",
            ],
        },
        {
            "dot": "slate",
            "title": "Source",
            "items": [
                "Real directories and real call/import edges from graph.db.",
                f"Tests excluded ({skipped_test_entities} entities).",
            ],
        },
    ]

    # --- curated chapters: real groupings, never one all-inclusive view ----
    chapters: dict[str, list[str]] = {}
    for comp_id in ordered:
        d = id_to_dir[comp_id]
        tail = labels[d].split("/")[-1]
        top_file = top_file_by_dir.get(d, (0, ""))[1]
        key = _chapter_for(_role_for(d), tail, top_file)
        chapters.setdefault(key, []).append(comp_id)

    views = [
        {
            "id": key,
            "label": _CHAPTER_META[key][0],
            "focus": chapters[key],
            "note": _CHAPTER_META[key][1],
        }
        for key in _CHAPTER_META
        if chapters.get(key)
    ]

    return {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": {
            "title": "Code architecture",
            "animation": "none",
            "views": views,
        },
        "layout": {"mode": "grid", "cols": 3, "cellW": 220, "cellH": 100,
                   "gapX": 20, "gapY": 20},
        "components": components,
        "connections": connections,
        "boundaries": [],
        "cards": cards,
        # id -> a real, RESOLVABLE code location for the box: the module's
        # largest file. Stripped from the IR before archify sees it
        # (archify_service), and carried on meta for the UI instead.
        "x_targets": {
            cid: top_file_by_dir[id_to_dir[cid]][1]
            for cid in ordered if id_to_dir[cid] in top_file_by_dir
        },
    }
