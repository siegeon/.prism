"""Ultimate Graph merge — one unified retrieval over Brain + Graph.

Prototype for the Ultimate Graph epic (siegeon/.prism#50), slice 4:
a single call that returns a focused, ranked, graph-aware view of the
codebase instead of forcing the caller to hit /api/brain/search and
the /api/graph/* family separately and stitch them by hand.

The insight from the issue: "brain search" and "graph view" are the
same query expressed two ways. Search is a *lens* (rank + focus) onto
the same entities + relationships + communities store the graph view
draws spatially. So we return both lenses from one payload:

    { query, mode, nodes[], edges[], communities[],
      ranked[{entity_id, score, why}],
      context[{file, outline, references, call_chain, chunks, annotations}],
      open_questions[], layout_hint, counts, provenance }

Empty query  -> mode="overview": the whole graph ranked by PageRank
                centrality (v6.1.5), file-level node/edge subgraph of
                the top hubs, plus the community list.
Typed query  -> mode="focus":   Brain hybrid search becomes the ranked
                list; the hit files + their 1-hop neighbor files form
                the subgraph; each top hit carries a context bundle
                (outline, callers, callees, matched chunks).

Backs both POST /api/brain/understand (the /explore SPA page) and the
`brain_understand` MCP tool — one code path, two front doors. Pure
read-through over existing services; no schema changes.

The narrative / annotation layer (graph_jobs, graph_annotate — slices
1, 2, 6 of the epic) is not built yet, so `annotations` and
`open_questions` come back as empty, provenance-ready arrays. The
contract is stable now; the enrichment loop fills those fields later
without changing the shape. Every node/edge here carries
provenance="deterministic" so the eventual LLM layer can never be
mistaken for structural truth.
"""

from __future__ import annotations

import os
from typing import Optional

from prism_service.project_context import get_project

# ForceAtlas2 hint the SPA can hand to the Sigma layout when it grows a
# live canvas. Tuned for the small focused subgraphs this endpoint
# returns (tens of nodes, not thousands).
_FA2_SETTINGS = {"gravity": 1.0, "scalingRatio": 8.0, "slowDown": 2.0}

# Cap on how many seed files we walk for 1-hop neighbors, and the total
# neighbor fan-out — keeps the focus subgraph legible and the SQL bounded.
_SEED_WALK_CAP = 12
_NEIGHBOR_CAP = 40
_CONTEXT_CAP = 6


def _basename(path: str) -> str:
    if not path:
        return ""
    return os.path.basename(path.replace("\\", "/"))


def _hit_file(r: dict) -> str:
    return r.get("source_file") or r.get("file") or r.get("source") or ""


def _hit_score(r: dict) -> float:
    try:
        return float(r.get("rrf_score") or r.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _file_community_map(communities: list[dict]) -> dict[str, int]:
    """Best-effort file -> community id from each community's top_files.

    Partial (top_files is a subset) but enough to tag focus-mode nodes
    and decide which communities are 'present' in a result.
    """
    out: dict[str, int] = {}
    for c in communities:
        for tf in c.get("top_files", []) or []:
            out.setdefault(tf, c["id"])
    return out


def build_understanding(
    project: str,
    query: Optional[str] = None,
    *,
    limit: int = 20,
    depth: int = 1,
) -> dict:
    """Build the unified understand payload for `project`.

    `query` empty/None -> overview; otherwise focus. `limit` bounds both
    the ranked list and (in focus mode) the Brain search width. `depth`
    >= 1 pulls 1-hop neighbor files into the focus subgraph.
    """
    ctx = get_project(project)
    brain = ctx.brain_svc
    graph = ctx.graph_svc

    communities = graph.communities()
    central_all = graph.top_central_entities(limit=200)

    q = (query or "").strip()
    if not q:
        return _overview(graph, communities, central_all, limit)
    return _focus(brain, graph, communities, central_all, q, limit, depth)


def _overview(graph, communities, central_all, limit: int) -> dict:
    """Whole-graph view ranked by centrality. Nodes + edges are the top
    hub *files* so the subgraph and the file-level edges line up."""
    ranked = [
        {
            "entity_id": f"{e.get('file', '')}::{e.get('name', '')}",
            "name": e.get("name", ""),
            "kind": e.get("kind", ""),
            "file": e.get("file", ""),
            "line": e.get("line"),
            "community": e.get("community"),
            "score": e.get("centrality", 0.0),
            "why": "Top hub by PageRank centrality",
        }
        for e in central_all[:limit]
    ]

    # Dedup central entities down to files, preserving centrality order.
    file_rows: list[dict] = []
    seen: set[str] = set()
    for e in central_all:
        f = e.get("file") or ""
        if f and f not in seen:
            seen.add(f)
            file_rows.append({
                "file": f,
                "centrality": e.get("centrality", 0.0),
                "community": e.get("community"),
            })
    top = file_rows[:limit]
    nodes = [
        {
            "id": r["file"],
            "label": _basename(r["file"]),
            "kind": "file",
            "community": r["community"],
            "centrality": r["centrality"],
            "seed": True,
            "provenance": "deterministic",
        }
        for r in top
    ]
    edges = [
        {**e, "provenance": "deterministic"}
        for e in graph.edges_between_files([r["file"] for r in top])
    ]
    return {
        "query": "",
        "mode": "overview",
        "nodes": nodes,
        "edges": edges,
        "communities": communities,
        "ranked": ranked,
        "context": [],
        "open_questions": [],
        "layout_hint": {"fa2_settings": _FA2_SETTINGS, "seed_positions": {}},
        "counts": {
            "nodes": len(nodes), "edges": len(edges),
            "communities": len(communities), "ranked": len(ranked),
        },
        "provenance": "deterministic",
    }


def _focus(brain, graph, communities, central_all, q: str,
           limit: int, depth: int) -> dict:
    """Typed-query view: Brain search is the ranked lens, its hit files +
    1-hop neighbors are the subgraph, top hits carry context bundles."""
    hits = brain.search(q, limit=limit) or []
    cent_by_file: dict[str, float] = {}
    for e in central_all:
        f = e.get("file") or ""
        if f:
            cent_by_file[f] = max(cent_by_file.get(f, 0.0),
                                  e.get("centrality", 0.0))
    file_comm = _file_community_map(communities)

    ranked: list[dict] = []
    seed_files: list[str] = []
    seen_files: set[str] = set()
    for r in hits:
        f = _hit_file(r)
        ranked.append({
            "entity_id": r.get("doc_id") or f"{f}::{r.get('entity_name', '')}",
            "name": r.get("entity_name") or r.get("doc_id") or _basename(f),
            "kind": r.get("entity_kind") or r.get("domain") or "",
            "file": f,
            "score": _hit_score(r),
            "why": (r.get("content") or r.get("body") or "")[:160].strip(),
        })
        if f and f not in seen_files:
            seen_files.add(f)
            seed_files.append(f)

    # 1-hop neighbor files via file-level call edges.
    neighbor_files: list[str] = []
    nbr_seen = set(seen_files)
    if depth >= 1:
        for f in seed_files[:_SEED_WALK_CAP]:
            fd = graph.file_detail(f)
            for row in (fd.get("inbound", []) + fd.get("outbound", [])):
                nf = row.get("from") or row.get("to")
                if nf and nf not in nbr_seen:
                    nbr_seen.add(nf)
                    neighbor_files.append(nf)
            if len(neighbor_files) >= _NEIGHBOR_CAP:
                neighbor_files = neighbor_files[:_NEIGHBOR_CAP]
                break

    all_files = seed_files + neighbor_files

    def _node(f: str, seed: bool) -> dict:
        return {
            "id": f,
            "label": _basename(f),
            "kind": "file",
            "community": file_comm.get(f),
            "centrality": cent_by_file.get(f, 0.0),
            "seed": seed,
            "provenance": "deterministic",
        }

    nodes = ([_node(f, True) for f in seed_files]
             + [_node(f, False) for f in neighbor_files])
    edges = [
        {**e, "provenance": "deterministic"}
        for e in graph.edges_between_files(all_files)
    ]

    present_ids = {n["community"] for n in nodes if n["community"] is not None}
    present = [c for c in communities if c["id"] in present_ids]

    context: list[dict] = []
    for f in seed_files[:_CONTEXT_CAP]:
        fd = graph.file_detail(f)
        chunks = [h["why"] for h in ranked if h["file"] == f and h["why"]][:3]
        context.append({
            "entity_id": f,
            "file": f,
            "community": file_comm.get(f),
            "outline": fd.get("entities", [])[:30],
            "references": fd.get("inbound", []),
            "call_chain": fd.get("outbound", []),
            "chunks": chunks,
            "annotations": [],  # narrative layer — filled by a later slice
        })

    return {
        "query": q,
        "mode": "focus",
        "nodes": nodes,
        "edges": edges,
        "communities": present,
        "ranked": ranked,
        "context": context,
        "open_questions": [],
        "layout_hint": {"fa2_settings": _FA2_SETTINGS, "seed_positions": {}},
        "counts": {
            "nodes": len(nodes), "edges": len(edges),
            "communities": len(present), "ranked": len(ranked),
            "seed_files": len(seed_files), "neighbor_files": len(neighbor_files),
        },
        "provenance": "deterministic",
    }
