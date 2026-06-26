"""Unit tests for the OKF concept GRAPH projection (services/okf_host.py).

The unified Understand wiki is modeled on Google's OKF visualizer: ONE
force-directed concept graph where nodes are memory concepts (colored by type)
and directed edges are the cross-links authored between them. OkfHost.graph()
derives that graph purely (read-only) from the projected memory bundle, and a
concept's get() carries inbound 'cited_by' backlinks so the detail panel can
show who references it.
"""

from prism_service.models.memory import ExpertiseEntry
from prism_service.services.okf_host import OkfHost


class _FakeMemory:
    def __init__(self, entries_by_domain):
        self._d = entries_by_domain

    def list_domains(self):
        return list(self._d)

    def list_entries(self, domain, **_):
        return self._d.get(domain, [])


def _mem():
    a = ExpertiseEntry(
        id="mx-1", name="task-titles", type="convention", classification="tactical",
        domain="conventions", summary="Titles are human-friendly.",
        description="See [[render-structured]] for the rule.",
        recorded_at="2026-06-26T00:00:00Z", importance=7, memory_type="procedural",
    )
    b = ExpertiseEntry(
        id="mx-2", name="render-structured", type="decision", classification="tactical",
        domain="conventions", summary="Render structured, never raw.",
        description="No pre/JSON dumps.", recorded_at="2026-06-26T00:00:00Z",
    )
    return _FakeMemory({"conventions": [a, b]})


def test_graph_nodes_carry_id_title_type():
    g = OkfHost(_mem()).graph()
    assert "nodes" in g and "edges" in g
    nodes = {n["id"]: n for n in g["nodes"]}
    assert set(nodes) == {"mx-1", "mx-2"}
    assert nodes["mx-1"]["title"] == "task-titles"
    assert nodes["mx-1"]["type"] == "convention"
    assert nodes["mx-2"]["type"] == "decision"


def test_graph_edge_derived_from_body_crosslink():
    g = OkfHost(_mem()).graph()
    # mx-1's body [[render-structured]] -> /memory/mx-2, so an edge mx-1 -> mx-2.
    edges = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("mx-1", "mx-2") in edges


def test_graph_edges_only_reference_known_nodes():
    g = OkfHost(_mem()).graph()
    ids = {n["id"] for n in g["nodes"]}
    for e in g["edges"]:
        assert e["source"] in ids and e["target"] in ids


def test_get_concept_carries_inbound_backlinks():
    host = OkfHost(_mem())
    got = host.get("/memory/conventions/render-structured.md")
    assert got is not None
    backlinks = got.get("backlinks")
    assert backlinks is not None
    cited_by_ids = {b["id"] for b in backlinks}
    # mx-1 cites mx-2, so mx-2's backlinks include mx-1.
    assert "mx-1" in cited_by_ids
    assert any(b["title"] == "task-titles" for b in backlinks)


def test_concept_with_no_inbound_links_has_empty_backlinks():
    host = OkfHost(_mem())
    got = host.get("/memory/conventions/task-titles.md")
    assert got is not None
    assert got.get("backlinks") == []
