"""Archify map builder: the concept map."""

from __future__ import annotations

from collections import defaultdict

from prism_service.project_context import get_project
from prism_service.services.okf_host import OkfHost
from prism_service.services.archify_maps._layout import place_grid, slug, clip

DIAGRAM_TYPE = "architecture"


def build(project: str, *, task_id: str | None = None) -> dict:
    """Build the domain concept map from curated memory (OKF graph)."""
    try:
        p = get_project(project)
        host = OkfHost(p.memory_svc, p.brain_svc)
        graph = host.graph()
    except Exception as exc:
        # Empty store; degrade gracefully
        return _empty_diagram(f"Failed to read memory: {exc}")

    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    if not nodes:
        return _empty_diagram("Memory store has no concepts. Run a PRISM session.")

    # Count edges per node; keep concepts with high degree + distribute by domain
    in_degree = defaultdict(int)
    out_degree = defaultdict(int)
    for edge in edges:
        out_degree[edge["source"]] += 1
        in_degree[edge["target"]] += 1

    # Group by domain; rank within each domain by total degree
    by_domain: dict[str, list] = defaultdict(list)
    for node in nodes:
        domain = node["domain"]
        degree = in_degree[node["id"]] + out_degree[node["id"]]
        by_domain[domain].append((node, degree))

    for domain in by_domain:
        by_domain[domain].sort(key=lambda x: -x[1])  # descending by degree

    # Keep top domains (by node count) and top concepts within each domain
    # Target ~40 components; roughly 4-5 per domain, scale down if needed
    top_domains = sorted(by_domain.keys(), key=lambda d: -len(by_domain[d]))[:5]
    kept_ids = set()
    groups = []
    domain_nodes = {}  # Keep the sorted node lists per domain

    # The concept graph is SPARSE — a few dozen cross-links across hundreds of
    # concepts — so ranking by degree inside a domain still picks mostly
    # unlinked concepts and draws a map with nothing joined up. Seed each
    # domain with the concepts that actually carry a link, then fill the rest.
    linked_ids = {e["source"] for e in edges} | {e["target"] for e in edges}

    for domain in top_domains:
        ranked = [n for n, _ in by_domain[domain]]
        linked_first = [n for n in ranked if n["id"] in linked_ids]
        rest = [n for n in ranked if n["id"] not in linked_ids]
        domain_group = (linked_first + rest)[:8]
        for node in domain_group:
            kept_ids.add(node["id"])
        if domain_group:
            domain_nodes[domain] = domain_group
            groups.append(domain_group)

    # Dedupe edges between kept concepts; prefer intra-domain to reduce crossing
    # Build domain map for filtering
    id_to_domain = {}
    for domain in domain_nodes:
        for node in domain_nodes[domain]:
            id_to_domain[node["id"]] = domain

    kept_edges = []
    seen = set()
    intra_domain = []  # Collect intra-domain edges separately
    inter_domain = []  # Then inter-domain edges

    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in kept_ids and tgt in kept_ids and (src, tgt) not in seen:
            seen.add((src, tgt))
            edge_obj = {"from": src, "to": tgt}
            if id_to_domain.get(src) == id_to_domain.get(tgt):
                intra_domain.append(edge_obj)
            else:
                inter_domain.append(edge_obj)

    # Prefer intra-domain edges, cap total at 80
    kept_edges = intra_domain[:40] + inter_domain[:40]
    kept_edges = kept_edges[:80]

    # Build components with archify id = slug(concept_id)
    components = []
    for group in groups:
        for node in group:
            c_id = slug(node["id"])
            c_type = _type_map(node["type"])
            # Extract concept id prefix (e.g., "mx-" from "mx-6320ab")
            # Use it as a shorter label alongside the type
            title_short = clip(node["title"], 18)  # Keep brief for grid rendering
            components.append({
                "id": c_id,
                "type": c_type,
                "label": title_short,
                "sublabel": node["type"],
                "tag": clip(node["domain"], 16),
            })

    # Place in grid
    group_ids = [[slug(n["id"]) for n in g] for g in groups]
    positions = place_grid(group_ids, cols=4)
    for comp in components:
        row, col = positions[comp["id"]]
        comp["row"] = row
        comp["col"] = col

    # Draw a link only between GRID-ADJACENT concepts. Archify refuses a route
    # that passes through an unrelated component, and a generated map cannot
    # hand-route waypoints, so a long edge is not a richer map — it is an
    # invalid one. Links that cannot be drawn are counted in the card instead.
    def _adjacent(a: str, b: str) -> bool:
        if a not in positions or b not in positions:
            return False
        (r1, c1), (r2, c2) = positions[a], positions[b]
        return abs(r1 - r2) + abs(c1 - c2) == 1

    connections = []
    drawn: set[frozenset] = set()
    for edge in kept_edges:
        a, b = slug(edge["from"]), slug(edge["to"])
        pair = frozenset((a, b))
        if pair in drawn or not _adjacent(a, b):
            continue
        drawn.add(pair)
        connections.append({"from": a, "to": b})
        if len(connections) >= 14:
            break
    undrawn = len(kept_edges) - len(connections)

    # Boundaries for each domain
    boundaries = []
    for domain in top_domains:
        if domain in domain_nodes:
            domain_ids = [slug(n["id"]) for n in domain_nodes[domain]]
            if domain_ids:
                boundaries.append({
                    "kind": "region",
                    "label": clip(domain, 24),
                    "wraps": domain_ids,
                })

    # Views for top domains
    views = []
    for i, domain in enumerate(top_domains[:5]):
        if domain in domain_nodes:
            domain_ids = [slug(n["id"]) for n in domain_nodes[domain]]
            if domain_ids:
                views.append({
                    "id": f"domain-{i}",
                    "label": clip(domain, 20),
                    "focus": domain_ids[:10],  # Focus top 10 per view
                    "note": f"Concepts in {domain} ({len(domain_ids)} total)",
                })

    # Summary cards
    cards = [
        {
            "dot": "cyan",
            "title": "Concept Map",
            "items": [
                f"Curated memory: {len(kept_ids)} of {len(nodes)} concepts.",
                f"Domains shown: {len(top_domains)} of {len(by_domain)}.",
                f"Links drawn: {len(connections)} of {len(kept_edges)}."
                + (f" {undrawn} link(s) join concepts too far apart to draw."
                   if undrawn > 0 else ""),
            ],
        },
    ]

    result = {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": {
            "title": "PRISM Concept Map",
            # NO TIMESTAMP HERE. Each publish diffs the redrawn map against
            # the one it replaced, and a clock in the subtitle makes every
            # publish report a change even when the architecture did not
            # move. The build time already reaches the reader through
            # meta.json, which the Maps panel renders as "built <time>".
            "subtitle": (
                f"{len(kept_ids)} concepts across {len(top_domains)} domains"
            ),
            "visual_preset": "blueprint",
            "animation": "none",
        },
        "layout": {
            "mode": "grid",
            "cols": 4,
            "cellW": 240,
            "cellH": 100,
            "gapX": 32,
            "gapY": 40,
        },
        "components": components,
        "connections": connections,
        "boundaries": boundaries,
        "cards": cards,
    }

    # Add views to meta if present
    if views:
        result["meta"]["views"] = views

    return result


def _type_map(concept_type: str) -> str:
    """Map concept type to archify component type."""
    mapping = {
        "decision": "security",
        "pattern": "backend",
        "convention": "frontend",
        "failure": "external",
    }
    return mapping.get(concept_type, "cloud")


def _empty_diagram(reason: str) -> dict:
    """Return a valid empty diagram when the store is empty."""
    return {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": {
            "title": "PRISM Concept Map",
            "subtitle": "Empty — no concepts yet",
            "visual_preset": "blueprint",
            "animation": "none",
        },
        "layout": {
            "mode": "grid",
            "cols": 1,
            "cellW": 170,
            "cellH": 76,
        },
        "components": [
            {
                "id": "empty",
                "type": "external",
                "label": "No data yet",
                "row": 0,
                "col": 0,
            }
        ],
        "cards": [
            {
                "dot": "slate",
                "title": "Empty Store",
                "items": [reason],
            }
        ],
    }
