"""Explore applies the ontology to the code graph (task 139a8131, epic
47bba8fe; owner, relayed verbatim: "please create a sub agent to appy the
ontoloft to expore and make sur ewe have cross clicking on everyy nooun
verb etc in the tasks").

GET /api/xref/neighbors attaches, per node, `ontology_class` (the o: class
the code-graph entity is typed with, via the SAME IRI shape
ontology_graph.py's _emit_code_graph builds; '' when the ontology graph
hasn't classified it yet -- honest, never guessed) and per edge
`ontology_property` (the graph.db relation kind when it's one of the 8
model.ttl declares as an o:relatesTo sub-property, else the honest
'relatesTo' catch-all).
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

# The PRISM SPA has NO JS test runner (tests/unit/test_conductor_page_
# animated_cleanup_ui.py's documented convention) -- UI acceptance criteria
# are pinned by asserting the ACTUAL TSX source.
_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_EXPLORE = (_SRC / "pages" / "ExplorePage.tsx").read_text(encoding="utf-8")
_ONTOLOGY = (_SRC / "pages" / "OntologyPage.tsx").read_text(encoding="utf-8")


def _seed_project(ctx) -> None:
    """foo calls baz; Caller calls foo -- all real graph.db entities/
    relationships, plus a docs row so the resolve ladder's symbol rung
    finds 'foo'. Mirrors test_brain_call_chain_relation_filter.py's
    pattern: touch brain_svc FIRST so Brain's own schema (all columns,
    including the confidence/call_site_* migrations) exists before writing
    rows directly, rather than guessing the schema ourselves."""
    from prism_service.config import project_data_dir

    ctx.brain_svc  # noqa: B018 -- triggers schema creation, see docstring
    data_dir = project_data_dir(ctx.project_id)

    conn = sqlite3.connect(str(data_dir / "brain.db"))
    # docs' own AFTER INSERT trigger (brain_engine.py) calls
    # expand_identifiers() to populate docs_fts -- registered on Brain's
    # OWN connection only, so a raw connection needs it too or the insert's
    # trigger fails with "no such function".
    from prism_service.engines.brain_engine import _expand_identifiers
    conn.create_function("expand_identifiers", 1, _expand_identifiers)
    conn.executemany(
        "INSERT INTO docs (id, source_file, content, entity_name, "
        "entity_kind, line_start, line_end) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("d1", "services/foo.py", "def foo(): pass", "foo", "function", 1, 2),
            ("d2", "services/caller.py", "def Caller(): pass", "Caller", "function", 1, 2),
        ],
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(data_dir / "graph.db"))
    conn.executemany(
        "INSERT INTO entities (id, name, kind, file) VALUES (?, ?, ?, ?)",
        [
            (1, "foo", "function", "services/foo.py"),
            (2, "baz", "function", "services/baz.py"),
            (3, "Caller", "function", "services/caller.py"),
        ],
    )
    conn.executemany(
        "INSERT INTO relationships (source_id, target_id, relation) "
        "VALUES (?, ?, ?)",
        [(1, 2, "calls"), (3, 1, "calls")],  # foo->baz, Caller->foo
    )
    conn.commit()
    conn.close()


def _client(monkeypatch, ctx):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import xref as xref_api

    monkeypatch.setattr(xref_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(xref_api.router, prefix="/api/xref")
    return TestClient(app)


@pytest.fixture
def project():
    from prism_service.project_context import get_project

    pid = f"explore-ontology-{uuid.uuid4().hex[:8]}"
    ctx = get_project(pid)
    _seed_project(ctx)
    return pid, ctx


def test_neighbors_carries_ontology_class_and_property_after_rebuild(project, monkeypatch):
    """After the ontology graph is rebuilt, the symbol center and its real
    code-graph neighbors carry ontology_class=Function (the SAME IRI shape
    _emit_code_graph builds), and edges carry ontology_property: the known
    'calls' relation maps to itself, the unmapped 'called by' display label
    honestly rolls up to 'relatesTo'."""
    pid, ctx = project
    from prism_service.services.ontology_graph import OntologyGraph

    OntologyGraph(pid).rebuild()

    client = _client(monkeypatch, ctx)
    body = client.get("/api/xref/neighbors",
                       params={"token": "foo", "project": pid}).json()

    assert body["center"]["kind"] == "code"
    assert body["center"]["ontology_class"] == "Function", body["center"]

    baz = next(n for n in body["neighbors"] if n["token"] == "baz")
    assert baz["ontology_class"] == "Function", baz
    caller = next(n for n in body["neighbors"] if n["token"] == "Caller")
    assert caller["ontology_class"] == "Function", caller

    calls_edges = [e for e in body["edges"] if e["label"] == "calls"]
    assert calls_edges, body["edges"]
    assert all(e["ontology_property"] == "calls" for e in calls_edges), body["edges"]

    called_by_edges = [e for e in body["edges"] if e["label"] == "called by"]
    assert called_by_edges, body["edges"]
    assert all(e["ontology_property"] == "relatesTo" for e in called_by_edges), body["edges"]


def test_neighbors_class_is_empty_before_a_rebuild(project, monkeypatch):
    """open_if_exists never creates a store: before any rebuild the ontology
    graph doesn't exist on disk yet, so every node honestly reads '' rather
    than guessing from graph.db's own kind column."""
    pid, ctx = project
    client = _client(monkeypatch, ctx)
    body = client.get("/api/xref/neighbors",
                       params={"token": "foo", "project": pid}).json()

    assert body["center"]["ontology_class"] == ""
    assert body["neighbors"], body
    for n in body["neighbors"]:
        assert n["ontology_class"] == "", n


def test_neighbors_class_empty_for_entity_added_after_rebuild(project, monkeypatch):
    """An entity real in graph.db but added AFTER the last ontology rebuild
    has no rdf:type triple yet -- class_of's own documented contract -- so
    it stays '' rather than being inferred from its live graph.db kind."""
    pid, ctx = project
    from prism_service.config import project_data_dir
    from prism_service.services.ontology_graph import OntologyGraph

    OntologyGraph(pid).rebuild()

    conn = sqlite3.connect(str(project_data_dir(pid) / "graph.db"))
    conn.execute(
        "INSERT INTO entities (id, name, kind, file) "
        "VALUES (4, 'qux', 'function', 'services/qux.py')"
    )
    conn.execute(
        "INSERT INTO relationships (source_id, target_id, relation) "
        "VALUES (1, 4, 'calls')"
    )
    conn.commit()
    conn.close()

    client = _client(monkeypatch, ctx)
    body = client.get("/api/xref/neighbors",
                       params={"token": "foo", "project": pid}).json()
    qux = next(n for n in body["neighbors"] if n["token"] == "qux")
    assert qux["ontology_class"] == "", qux


def test_ontology_property_mapping_table():
    """The edge-kind -> o:property mapping: the 8 relation kinds graph.db
    actually emits (matching model.ttl's o:relatesTo sub-properties) map to
    themselves; anything else -- including a bare/missing label -- rolls up
    to the honest 'relatesTo' catch-all."""
    from prism_service.api import xref

    for kind in ("calls", "imports", "imports_from", "inherits",
                 "contains", "uses", "method", "rationale_for"):
        assert xref._ontology_property_for(kind) == kind
    assert xref._ontology_property_for("recalled") == "relatesTo"
    assert xref._ontology_property_for("called by") == "relatesTo"
    assert xref._ontology_property_for(None) == "relatesTo"
    assert xref._ontology_property_for("CALLS") == "calls"


def test_explore_page_renders_the_ontology_class_pill_linking_to_ontology():
    """The Selected rail's class pill uses the existing .ont-node primitive
    (data-kind="class", the square/var(--formal) glyph OntologyPage.tsx's
    own Structure tree already uses) and deep-links to
    /ontology?tab=structure&class=<cls>, bound to center.ontology_class."""
    assert 'ontology_class' in _EXPLORE
    assert 'className="ont-node" data-kind="class"' in _EXPLORE
    assert "/ontology?tab=structure&class=" in _EXPLORE
    assert "unclassified" in _EXPLORE


def test_explore_page_edge_rendering_reads_ontology_property():
    """Edge tooltips/labels read ontology_property, not just the raw
    center->neighbor relation label."""
    assert "ontology_property" in _EXPLORE


def test_explore_page_legend_groups_by_ontology_class():
    """The legend groups by ontology_class when Explore has classified
    something in view, falling back to the plain kind-shape key."""
    assert "ontologyLegend" in _EXPLORE


def test_ontology_page_reads_tab_and_class_query_params():
    """OntologyPage.tsx accepts ?tab=&class= (query-param READ only, tab
    winning over the localStorage default; class scrolls/highlights the
    matching Structure row)."""
    assert "useSearchParams" in _ONTOLOGY
    assert 'searchParams.get("tab")' in _ONTOLOGY
    assert 'searchParams.get("class")' in _ONTOLOGY
    assert "scrollIntoView" in _ONTOLOGY
