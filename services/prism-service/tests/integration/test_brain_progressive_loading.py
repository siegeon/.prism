"""RED integration tests for /brain progressive graph loading.

Pins the MOST machine-testable acceptance criteria of task b521af05
("Brain page: load the graph progressively instead of the whole graph
upfront") against the backend Sigma-viewer endpoints in
``routes/graph_static.py``. These tests are RED on purpose — the
progressive-loading features do not exist yet:

  * AC-1 / FR-1 (cap)      — ``hierarchy.json`` returns EVERY node; there
                             is no top-N / ``?limit=`` cap, so the initial
                             payload is unbounded.
  * AC-4 / FR-4 (ETag/304) — the handler returns ``Cache-Control: no-store``
                             with NO ``ETag``, so a conditional re-GET is a
                             full re-download, never a ``304 Not Modified``.
  * AC-2 / FR-2 (neighbors)— there is no ``/neighbors`` expand-on-demand
                             route, so drill-in cannot fetch just a hub's
                             neighborhood — the route 404s.

The suite-wide ``PRISM_DATA_DIR`` isolation (tests/conftest.py) gives us a
hermetic throwaway store, so the fixture SEEDS a small deterministic
``graph.json`` into the project's ``graphify-src/graphify-out`` dir. That
makes the hierarchy endpoint return 200 with a KNOWN node count, so the
cap / ETag / neighbors assertions fail because the FEATURE is missing —
not because data is missing.
"""

import json

import pytest

PROJECT = "prism"

# A deterministic seeded graph: one hub (``n0``) wired to 24 leaves, plus a
# handful of unconnected nodes. Total 30 nodes — comfortably above any small
# cap we assert, so an uncapped response is unambiguously RED.
_HUB = "n0"
_LEAF_COUNT = 24
_TOTAL_NODES = 30
_CAP = 5  # top-N the capped endpoint must honor (well below 30)


def _seed_graph(data_dir):
    nodes = []
    for i in range(_TOTAL_NODES):
        nodes.append({
            "id": f"n{i}",
            "source_file": f"prism_service/services/mod{i % 6}/file{i}.py",
            "community": i % 4,
            "file_type": "code",
        })
    # Star topology: the hub is the highest-degree node, so a degree/centrality
    # cap must keep it and a neighbors(hub, 1) query has a bounded, non-trivial
    # neighborhood.
    edges = [{"source": _HUB, "target": f"n{i}"} for i in range(1, _LEAF_COUNT + 1)]
    graph = {"nodes": nodes, "links": edges}

    out_dir = data_dir / "graphify-src" / "graphify-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from prism_service.main import app
    from prism_service.project_context import get_project

    ctx = get_project(PROJECT)
    _seed_graph(ctx._data_dir)
    return TestClient(app)


def _hierarchy_url(project=PROJECT):
    return f"/graphify-visual/{project}/hierarchy.json"


def test_hierarchy_payload_is_capped_to_top_n(client):
    """AC-1 / FR-1 — the initial payload must be bounded to the top-N hubs,
    never the whole graph. Today the handler returns every seeded node."""
    r = client.get(_hierarchy_url(), params={"limit": _CAP})
    assert r.status_code == 200, r.text
    nodes = r.json().get("nodes")
    assert isinstance(nodes, list) and nodes, "expected a non-empty node list from seeded graph"
    # RED: today the endpoint ignores the cap and returns all _TOTAL_NODES.
    assert len(nodes) <= _CAP, (
        f"hierarchy.json returned {len(nodes)} nodes for limit={_CAP}; "
        "the initial payload is not capped to the top-N hubs (FR-1)."
    )


def test_hierarchy_supports_etag_conditional_get(client):
    """AC-4 / FR-4 — a repeat visit must revalidate conditionally and get a
    304, not re-download. Today the handler sets no-store and emits no ETag."""
    r1 = client.get(_hierarchy_url())
    assert r1.status_code == 200, r1.text

    # RED: no ETag is emitted today (Cache-Control: no-store instead).
    etag = r1.headers.get("ETag")
    assert etag, (
        "hierarchy.json emitted no ETag response header; a repeat visit "
        "cannot be a conditional GET (FR-4). "
        f"Cache-Control={r1.headers.get('Cache-Control')!r}"
    )

    r2 = client.get(_hierarchy_url(), headers={"If-None-Match": etag})
    # RED: today the second GET is a full 200 re-download, never a 304.
    assert r2.status_code == 304, (
        f"conditional GET with If-None-Match returned {r2.status_code}, "
        "expected 304 Not Modified (FR-4)."
    )


def test_neighbors_expand_on_demand_endpoint_exists(client):
    """AC-2 / FR-2 — drill-in must fetch only a hub's neighborhood from a
    server ``/neighbors`` route. Today that route does not exist -> 404."""
    r = client.get(
        f"/graphify-visual/{PROJECT}/neighbors",
        params={"node": _HUB, "hops": 1},
    )
    # RED: the neighbors expand-on-demand route is not implemented yet.
    assert r.status_code == 200, (
        f"GET /graphify-visual/{PROJECT}/neighbors returned {r.status_code}; "
        "the expand-on-demand neighbors endpoint does not exist yet (FR-2)."
    )
    body = r.json()
    nbr_nodes = body.get("nodes")
    assert isinstance(nbr_nodes, list) and nbr_nodes, body
    # A 1-hop neighborhood of the hub is bounded: the hub + its direct leaves,
    # NOT the whole graph.
    assert len(nbr_nodes) <= _LEAF_COUNT + 1, (
        f"neighbors returned {len(nbr_nodes)} nodes; a 1-hop neighborhood "
        "must be a bounded subgraph, not the whole graph (FR-2)."
    )
