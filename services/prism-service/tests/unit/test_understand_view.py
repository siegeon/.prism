"""Tests for v6.1.6 Ultimate Graph merge — understand_view.build_understanding
(siegeon/.prism#50, slices 4+5).

The function is a pure read-through over brain_svc + graph_svc, so we
stub both and assert the assembled contract: overview ranks by
centrality, focus uses search hits + 1-hop neighbors + context bundles,
and provenance is stamped on every structural item.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class _FakeBrain:
    def __init__(self, hits):
        self._hits = hits
        self.last_query = None

    def search(self, query, domain=None, limit=20, domains=None):
        self.last_query = query
        return self._hits[:limit]


class _FakeGraph:
    def __init__(self, central, communities, details, annotations=None):
        self._central = central
        self._communities = communities
        self._details = details  # file -> file_detail dict
        # annotations: {scope_kind: {scope_id: {name, purpose, provenance, updated_at}}}
        self._annotations = annotations or {}

    def annotations_for(self, scope_kind, task="name"):
        return dict(self._annotations.get(scope_kind, {}))

    def communities(self):
        return self._communities

    def top_central_entities(self, limit=20):
        return self._central[:limit]

    def file_detail(self, path):
        return self._details.get(path, {"entities": [], "inbound": [], "outbound": []})

    def file_communities(self, files):
        s = set(files)
        m = {}
        for e in self._central:
            f = e.get("file")
            if f in s and e.get("community") is not None:
                m.setdefault(f, e["community"])
        return m

    def edges_between_files(self, paths):
        s = set(paths)
        out = []
        for p in paths:
            for row in self._details.get(p, {}).get("outbound", []):
                if row["to"] in s:
                    out.append({"from": p, "to": row["to"], "weight": row["weight"]})
        return out


class _FakeCtx:
    def __init__(self, brain, graph):
        self.brain_svc = brain
        self.graph_svc = graph


def _wire(monkeypatch, brain, graph):
    from prism_service.services import understand_view
    monkeypatch.setattr(understand_view, "get_project",
                        lambda project: _FakeCtx(brain, graph))
    return understand_view


_CENTRAL = [
    {"name": "GraphService", "kind": "class", "file": "a.py", "line": 10, "community": 1, "centrality": 0.9},
    {"name": "rebuild", "kind": "method", "file": "a.py", "line": 50, "community": 1, "centrality": 0.5},
    {"name": "helper", "kind": "function", "file": "b.py", "line": 3, "community": 2, "centrality": 0.2},
]
_COMMS = [
    {"id": 1, "label": "Graph core", "size": 12, "summary": "the graph pipeline", "top_files": ["a.py"], "top_entities": []},
    {"id": 2, "label": "Utils", "size": 4, "summary": "helpers", "top_files": ["b.py"], "top_entities": []},
]
_DETAILS = {
    "a.py": {"entities": [{"name": "GraphService", "kind": "class", "line": 10}],
             "inbound": [{"from": "c.py", "weight": 2}],
             "outbound": [{"to": "b.py", "weight": 3}]},
    "b.py": {"entities": [{"name": "helper", "kind": "function", "line": 3}],
             "inbound": [{"from": "a.py", "weight": 3}], "outbound": []},
}


def test_overview_ranks_by_centrality(monkeypatch):
    uv = _wire(monkeypatch, _FakeBrain([]), _FakeGraph(_CENTRAL, _COMMS, _DETAILS))
    out = uv.build_understanding("p", None)
    assert out["mode"] == "overview"
    assert out["query"] == ""
    # ranked is centrality-ordered, top first
    assert [r["name"] for r in out["ranked"]][:2] == ["GraphService", "rebuild"]
    assert out["ranked"][0]["score"] == 0.9
    # nodes are file-level, deduped (a.py once though it has two hubs)
    assert [n["id"] for n in out["nodes"]] == ["a.py", "b.py"]
    assert all(n["provenance"] == "deterministic" for n in out["nodes"])
    assert len(out["communities"]) == 2
    assert out["context"] == []


def test_focus_uses_hits_and_one_hop_neighbors(monkeypatch):
    hits = [
        {"source_file": "a.py", "entity_name": "GraphService", "entity_kind": "class",
         "rrf_score": 0.42, "content": "the graph service rebuilds communities"},
    ]
    uv = _wire(monkeypatch, _FakeBrain(hits), _FakeGraph(_CENTRAL, _COMMS, _DETAILS))
    out = uv.build_understanding("p", "graph rebuild")
    assert out["mode"] == "focus"
    assert out["query"] == "graph rebuild"
    # ranked from the search hit
    assert out["ranked"][0]["name"] == "GraphService"
    assert out["ranked"][0]["score"] == 0.42
    # seed file a.py + 1-hop neighbors (b.py outbound, c.py inbound)
    seed = [n["id"] for n in out["nodes"] if n["seed"]]
    nbr = [n["id"] for n in out["nodes"] if not n["seed"]]
    assert seed == ["a.py"]
    assert set(nbr) == {"b.py", "c.py"}
    # context bundle carries outline + callers + callees + matched chunk
    ctx = out["context"][0]
    assert ctx["file"] == "a.py"
    assert ctx["outline"][0]["name"] == "GraphService"
    assert ctx["references"] == [{"from": "c.py", "weight": 2}]
    assert ctx["call_chain"] == [{"to": "b.py", "weight": 3}]
    assert ctx["chunks"] and ctx["chunks"][0].startswith("the graph service")
    # annotations / open_questions are empty but present (narrative layer later)
    assert ctx["annotations"] == []
    assert out["open_questions"] == []


def test_focus_depth_zero_skips_neighbors(monkeypatch):
    hits = [{"source_file": "a.py", "entity_name": "GraphService", "rrf_score": 0.1, "content": "x"}]
    uv = _wire(monkeypatch, _FakeBrain(hits), _FakeGraph(_CENTRAL, _COMMS, _DETAILS))
    out = uv.build_understanding("p", "graph", depth=0)
    assert [n["id"] for n in out["nodes"]] == ["a.py"]
    assert all(n["seed"] for n in out["nodes"])


def test_empty_query_string_is_overview(monkeypatch):
    uv = _wire(monkeypatch, _FakeBrain([]), _FakeGraph(_CENTRAL, _COMMS, _DETAILS))
    assert uv.build_understanding("p", "   ")["mode"] == "overview"


def test_seed_files_cluster_clickthrough(monkeypatch):
    """Clicking a cluster posts its member files; the payload is focus,
    ranked by centrality within the set, with 1-hop neighbors + context."""
    uv = _wire(monkeypatch, _FakeBrain([]), _FakeGraph(_CENTRAL, _COMMS, _DETAILS))
    out = uv.build_understanding("p", seed_files=["a.py"], label="Graph core")
    assert out["mode"] == "focus"
    assert out["query"] == "Graph core"
    # a.py's entities ranked by centrality, no Brain search involved
    assert out["ranked"][0]["name"] == "GraphService"
    assert out["ranked"][0]["why"] == "in Graph core"
    seed = [n["id"] for n in out["nodes"] if n["seed"]]
    nbr = [n["id"] for n in out["nodes"] if not n["seed"]]
    assert seed == ["a.py"]
    assert set(nbr) == {"b.py", "c.py"}  # 1-hop from a.py
    assert out["context"][0]["file"] == "a.py"


def test_seed_files_capped_to_top_central_hubs(monkeypatch):
    """A big cluster click (hundreds of files) caps seeds to the most
    central hubs and reports the true total, so the panel stays legible."""
    central = [{"name": f"e{i}", "kind": "function", "file": f"f{i}.py",
                "line": 1, "community": 1, "centrality": (200 - i) / 1000.0}
               for i in range(200)]
    details = {f"f{i}.py": {"entities": [], "inbound": [], "outbound": []}
               for i in range(200)}
    comms = [{"id": 1, "label": "Big", "size": 200, "summary": "",
              "top_files": [], "top_entities": []}]
    uv = _wire(monkeypatch, _FakeBrain([]), _FakeGraph(central, comms, details))
    files = [f"f{i}.py" for i in range(200)]
    out = uv.build_understanding("p", seed_files=files, label="Big")
    seeds = [n for n in out["nodes"] if n["seed"]]
    assert len(seeds) == 40                          # capped (_MAX_SEEDS)
    assert out["counts"]["total_seed_files"] == 200  # true total reported
    # the kept seeds are the most central (f0..f39 have highest centrality)
    assert seeds[0]["id"] == "f0.py"


def test_seed_files_take_precedence_over_query(monkeypatch):
    hits = [{"source_file": "b.py", "entity_name": "helper", "rrf_score": 0.9, "content": "x"}]
    uv = _wire(monkeypatch, _FakeBrain(hits), _FakeGraph(_CENTRAL, _COMMS, _DETAILS))
    out = uv.build_understanding("p", "helper", seed_files=["a.py"], label="Graph core")
    # seed_files wins — seeded by a.py, not the brain hit b.py
    assert [n["id"] for n in out["nodes"] if n["seed"]] == ["a.py"]


# --- Narrative annotation layer (Background-Agent pull-loop, #50) -----------
# build_understanding must join graph.annotations_for(...) into the per-file
# context 'annotations' field (was hardcoded [] at understand_view.py:339),
# keyed by scope, for all three scope kinds — 'node', 'community', 'hierarchy'.
# Additive + contract-preserving; each annotation carries provenance that
# discriminates 'deterministic' from the LLM literal 'claude @ <date>'.

_ANNOTATIONS = {
    # node scope keys on the file path itself (no migration — free-text TEXT)
    "node": {
        "a.py": {"name": "Graph Service", "purpose": "Owns the code graph.",
                 "provenance": "claude @ 2026-05-29",
                 "updated_at": "2026-05-29T00:00:00+00:00"},
    },
    # community scope keys on the community id (as string)
    "community": {
        "1": {"name": "Graph Core", "purpose": "The graph pipeline.",
              "provenance": "claude @ 2026-05-29",
              "updated_at": "2026-05-29T00:00:00+00:00"},
    },
    # hierarchy scope keys on the hierarchy path key
    "hierarchy": {
        "a.py": {"name": "Backend", "purpose": "Service backend.",
                 "provenance": "deterministic",
                 "updated_at": "2026-05-29T00:00:00+00:00"},
    },
}


def test_focus_joins_node_scope_annotation(monkeypatch):
    """The per-file context bundle joins the node-scope annotation for that
    file (was a hardcoded empty list) — name/purpose/provenance/updated_at."""
    hits = [{"source_file": "a.py", "entity_name": "GraphService",
             "rrf_score": 0.42, "content": "x"}]
    uv = _wire(monkeypatch, _FakeBrain(hits),
               _FakeGraph(_CENTRAL, _COMMS, _DETAILS, _ANNOTATIONS))
    out = uv.build_understanding("p", "graph")
    ctx = out["context"][0]
    assert ctx["file"] == "a.py"
    anns = ctx["annotations"]
    assert anns, "node-scope annotation must be joined, not left empty"
    node = [a for a in anns if a.get("scope_kind") == "node"]
    assert node and node[0]["name"] == "Graph Service"
    assert node[0]["purpose"] == "Owns the code graph."
    assert node[0]["provenance"] == "claude @ 2026-05-29"
    assert node[0]["updated_at"] == "2026-05-29T00:00:00+00:00"


def test_focus_joins_community_and_hierarchy_scopes(monkeypatch):
    """All three scope kinds are wired: a.py is in community 1, so the
    community-scope annotation surfaces alongside node + hierarchy."""
    hits = [{"source_file": "a.py", "entity_name": "GraphService",
             "rrf_score": 0.42, "content": "x"}]
    uv = _wire(monkeypatch, _FakeBrain(hits),
               _FakeGraph(_CENTRAL, _COMMS, _DETAILS, _ANNOTATIONS))
    out = uv.build_understanding("p", "graph")
    kinds = {a["scope_kind"] for a in out["context"][0]["annotations"]}
    assert {"node", "community", "hierarchy"} <= kinds


def test_annotation_provenance_discriminates_llm_from_deterministic(monkeypatch):
    """Provenance on each annotation distinguishes 'deterministic' from the
    LLM literal 'claude @ <date>' so the narrative can't be mistaken for
    structural truth."""
    hits = [{"source_file": "a.py", "entity_name": "GraphService",
             "rrf_score": 0.42, "content": "x"}]
    uv = _wire(monkeypatch, _FakeBrain(hits),
               _FakeGraph(_CENTRAL, _COMMS, _DETAILS, _ANNOTATIONS))
    out = uv.build_understanding("p", "graph")
    anns = out["context"][0]["annotations"]
    provs = {a["provenance"] for a in anns}
    assert "deterministic" in provs
    assert any(p.startswith("claude @ ") for p in provs)


def test_annotations_empty_when_store_empty(monkeypatch):
    """Contract preserved: with no annotations stored, the field is still a
    present empty list (additive, never breaks the shape)."""
    hits = [{"source_file": "a.py", "entity_name": "GraphService",
             "rrf_score": 0.42, "content": "x"}]
    uv = _wire(monkeypatch, _FakeBrain(hits),
               _FakeGraph(_CENTRAL, _COMMS, _DETAILS))  # no annotations
    out = uv.build_understanding("p", "graph")
    assert out["context"][0]["annotations"] == []
