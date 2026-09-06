"""Archify map builder: the language map.

PRISM's ontology is the language layer of Understand: the CLASSES the system
talks about, the PROPERTIES that relate them, and the RULES that constrain
them. The map draws the classes and their relations, and names on cards which
rules currently hold and which fail.

A class with no instances is still part of the language, so the vocabulary is
read from the properties as well as from the materialized class list. Drawing
only the classes that happen to have rows today would make the map's shape
depend on today's data rather than on the language itself.
"""

from __future__ import annotations

from prism_service.services.archify_maps._layout import clip, place_grid, slug

DIAGRAM_TYPE = "architecture"

# A range that names a literal datatype or a raw IRI, not a class we can draw.
_LITERAL_HINTS = ("XMLSchema", "#", "http://", "https://", "urn:")

_MAX_CLASSES = 22
_MAX_CONNECTIONS = 14


def _is_class_name(name) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    return not any(hint in name for hint in _LITERAL_HINTS)


def _infer_type(name: str, description: str = "") -> str:
    text = f"{name} {description}".lower()
    if any(w in text for w in ("code", "file", "module", "function", "class",
                               "method", "document", "folder")):
        return "database"
    if any(w in text for w in ("party", "person", "group", "agent", "provider",
                               "channel")):
        return "external"
    if any(w in text for w in ("signal", "event", "message")):
        return "messagebus"
    if any(w in text for w in ("gate", "policy", "rule", "law", "term")):
        return "security"
    if any(w in text for w in ("task", "work", "activity", "ask", "job")):
        return "backend"
    return "cloud"


def _empty(reason: str) -> dict:
    return {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": {
            "title": "Language",
            "subtitle": reason,
            "visual_preset": "blueprint",
            "animation": "none",
        },
        "layout": {"mode": "grid", "cols": 1, "cellW": 170, "cellH": 76},
        "components": [
            {"id": "empty", "type": "external", "label": "No data yet",
             "row": 0, "col": 0},
        ],
        "cards": [{"dot": "slate", "title": "Nothing to draw", "items": [reason]}],
    }


def build(project: str, *, task_id: str | None = None) -> dict:
    from prism_service.services.ontology_graph import OntologyGraph

    try:
        graph = OntologyGraph(project)
        classes = graph.classes()
        properties = graph.properties()
    except Exception as exc:
        return _empty(f"The ontology store could not be read: {exc}")

    if not classes and not properties:
        return _empty("The ontology has no classes. Rebuild it on the Ontology tab.")

    # Vocabulary = the materialized classes, plus every class a property names.
    described: dict[str, dict] = {}
    for c in classes:
        cid = c.get("id")
        if _is_class_name(cid):
            described[cid] = c

    relations: list[tuple[str, str, str]] = []
    for p in properties:
        src, dst = p.get("domain_class"), p.get("range_class")
        if not _is_class_name(src) or not _is_class_name(dst):
            continue
        relations.append((src, dst, str(p.get("name") or "")))
        described.setdefault(src, {"id": src, "name": src, "instance_count": 0})
        described.setdefault(dst, {"id": dst, "name": dst, "instance_count": 0})

    # A class that carries relations earns its place before a lone one does.
    degree: dict[str, int] = {}
    for src, dst, _name in relations:
        degree[src] = degree.get(src, 0) + 1
        degree[dst] = degree.get(dst, 0) + 1

    ranked = sorted(
        described.values(),
        key=lambda c: (-degree.get(c["id"], 0), -int(c.get("instance_count") or 0)),
    )[:_MAX_CLASSES]
    kept = {c["id"] for c in ranked}

    components = []
    for c in ranked:
        count = int(c.get("instance_count") or 0)
        comp = {
            "id": slug(c["id"]),
            "type": _infer_type(str(c.get("name") or c["id"]),
                                str(c.get("description") or "")),
            "label": clip(str(c.get("name") or c["id"]), 26),
            "sublabel": f"{count} instances" if count else "no instances yet",
        }
        source = c.get("source")
        if isinstance(source, str) and source:
            comp["tag"] = clip(source, 16)
        components.append(comp)

    positions = place_grid([[c["id"] for c in components]], cols=4)
    for comp in components:
        comp["row"], comp["col"] = positions[comp["id"]]

    # Draw a relation only where the grid puts the two classes side by side.
    # Archify refuses a route that crosses an unrelated component, and this map
    # is generated, so it cannot hand-route waypoints the way an authored
    # diagram does. A relation that cannot be drawn is still counted on a card.
    def _adjacent(a: str, b: str) -> bool:
        if a not in positions or b not in positions:
            return False
        (r1, c1), (r2, c2) = positions[a], positions[b]
        return abs(r1 - r2) + abs(c1 - c2) == 1

    by_pair: dict[tuple[str, str], list[str]] = {}
    for src, dst, name in relations:
        if src not in kept or dst not in kept or src == dst:
            continue  # a self-relation has no line to draw
        by_pair.setdefault((slug(src), slug(dst)), []).append(name)

    # The line carries NO label. On a generated grid the midpoint of a short
    # route lands on a component box, which archify refuses, and no labelDx /
    # labelDy / labelSegment offset clears it for every pair. The relation
    # names are listed on their own card instead, where they stay readable.
    connections = []
    named: list[str] = []
    for (a, b), names in by_pair.items():
        if not _adjacent(a, b):
            continue
        connections.append({"from": a, "to": b})
        named.append(f"{a} → {b}: " + ", ".join(sorted(set(names))[:3]))
        if len(connections) >= _MAX_CONNECTIONS:
            break

    # The rules are the point of this map, so they are named, never counted.
    holds: list[str] = []
    fails: list[str] = []
    try:
        from prism_service.services import rule_decisions
        for r in rule_decisions.decorated_report(project).get("rules", []):
            title = clip(str(r.get("title") or r.get("name") or "rule"), 52)
            violations = int(r.get("violations") or 0)
            if violations:
                fails.append(f"{title} — {violations} violations")
            else:
                holds.append(title)
    except Exception:
        pass

    cards = [{
        "dot": "cyan",
        "title": "Vocabulary",
        "items": [
            f"{len(components)} of {len(described)} classes drawn.",
            f"{len(relations)} relations between classes; {len(connections)} drawn.",
            f"{len(properties)} properties in total.",
        ],
    }]
    if named:
        cards.append({"dot": "violet", "title": "Relations drawn", "items": named[:8]})
    if holds:
        cards.append({"dot": "emerald", "title": "Rules that hold",
                      "items": holds[:8]})
    if fails:
        cards.append({"dot": "rose", "title": "Rules that fail",
                      "items": fails[:8]})

    views = []
    for kind in ("backend", "database", "external", "messagebus", "security"):
        focus = [c["id"] for c in components if c["type"] == kind]
        if len(focus) >= 2:
            views.append({
                "id": slug(f"lens-{kind}"),
                "label": kind.title(),
                "focus": focus,
                "note": f"{len(focus)} classes of this kind",
            })

    meta = {
        "title": "Language",
        "subtitle": (
            f"PRISM's ontology: {len(described)} classes, {len(properties)} "
            f"properties, {len(holds) + len(fails)} rules"
        ),
        "visual_preset": "blueprint",
        "animation": "none",
    }
    if views:
        meta["views"] = views[:5]

    return {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": meta,
        "layout": {"mode": "grid", "cols": 4, "cellW": 190, "cellH": 84,
                   "gapX": 28, "gapY": 34},
        "components": components,
        "connections": connections,
        "cards": cards,
    }
