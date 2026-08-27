"""The ontology holds the whole code graph (task f9e0745e, epic 61821448
"Understand writes the law, the ontology holds it, the code obeys it").

Measured on the prism project itself (2026-08-26): the ontology projected
155 Code nodes and ZERO edges, even though graph.db held 137k
relationships. Root cause (other half of this task, see
test_explore_indexes_source_not_bundles.py): almost every graph.db
entity was a stale web_dist bundle row, and even the real ones only
contributed a name via a capped 50-row-per-kind SAMPLE with no edges at
all -- OntologyGraph._emit_code_graph never read graph.db's relationships
table.

This file seeds a real graph.db (two functions, a class with a method, a
call edge, an import edge, and a rationale edge) and pins:
  - every real symbol is typed (o:Function/o:Class/o:Method/o:Rationale)
  - every real edge is projected (o:calls, o:imports, o:method,
    o:rationale_for)
  - structure() rolls the counts up to o:Code correctly
  - a second rebuild does not double the triple count (remove_graph +
    bulk_load, never an incremental add)
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _seed_code_graph(pid: str) -> None:
    """func_a calls func_b; func_a imports Widget; Widget has method
    build; a rationale row explains func_a. All under real (non-skipped)
    paths, real graph.db schema (entities.file/line, relationships.
    source_id/target_id/relation) -- the production shape, not a
    stripped-down test fixture."""
    from prism_service.config import project_data_dir

    graph_db = project_data_dir(pid) / "graph.db"
    conn = sqlite3.connect(str(graph_db))
    conn.executescript("""
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY, name TEXT, kind TEXT, file TEXT, line INTEGER
        );
        CREATE TABLE relationships (
            source_id INTEGER, target_id INTEGER, relation TEXT
        );
    """)
    conn.executemany(
        "INSERT INTO entities (id, name, kind, file, line) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "func_a", "function", "services/foo.py", 10),
            (2, "func_b", "function", "services/foo.py", 20),
            (3, "Widget", "class", "services/widget.py", 5),
            (4, "build", "method", "services/widget.py", 8),
            (5, "explains why func_a exists", "rationale", "services/foo.py", 9),
            # A bundle row -- must never become an ontology instance or
            # take part in any edge (test_explore_indexes_source_not_
            # bundles.py covers the purge; this pins the projection side).
            (6, "bundle_fn", "function",
             "prism_service/web_dist/assets/index-abc.js", 1),
            # task 2bfe49db: a module row whose file sits under the package
            # root -- its label must be the PACKAGE-RELATIVE path, the same
            # label law_check emits over a diff.
            (7, "x.py", "module",
             "services/prism-service/prism_service/models/x.py", 1),
        ],
    )
    conn.executemany(
        "INSERT INTO relationships (source_id, target_id, relation) VALUES (?, ?, ?)",
        [
            (1, 2, "calls"),        # func_a calls func_b
            (1, 3, "imports"),      # func_a imports Widget
            (3, 4, "method"),       # Widget has method build
            (5, 1, "rationale_for"),  # the rationale explains func_a
            (6, 1, "calls"),        # a bundle edge -- must not appear
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def project():
    from prism_service.project_context import get_project

    pid = f"code-graph-{uuid.uuid4().hex[:8]}"
    get_project(pid)
    _seed_code_graph(pid)
    return pid


def _ask(og, sparql_body: str) -> bool:
    from prism_service.services.ontology_graph import NS

    sparql = (
        f"PREFIX o: <{NS}> "
        f"PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
        f"ASK {{ GRAPH ?g {{ {sparql_body} }} }}"
    )
    out = og.query(sparql)
    return out["bindings"][0]["ask"] == "True"


def test_every_real_symbol_is_typed(project):
    from prism_service.services.ontology_graph import OntologyGraph

    og = OntologyGraph(project)
    result = og.rebuild()

    assert og._count("Function") == 2
    assert og._count("Class") == 1
    assert og._count("Method") == 1
    assert og._count("Rationale") == 1
    # The bundle row never becomes ANY ontology instance.
    assert og._count("Code") == 0  # nothing typed as the bare fallback here
    assert result["per_class"]["CodeGraph::Function"] == 2
    assert result["per_class"]["CodeGraph::Class"] == 1
    assert result["per_class"]["CodeGraph::Method"] == 1
    assert result["per_class"]["CodeGraph::Rationale"] == 1
    assert "CodeGraph::Code" not in result["per_class"]


def test_every_real_edge_is_projected(project):
    from prism_service.services.ontology_graph import OntologyGraph

    og = OntologyGraph(project)
    og.rebuild()

    assert _ask(og, "?a a o:Function ; rdfs:label 'func_a' ."
                     " ?b a o:Function ; rdfs:label 'func_b' ."
                     " ?a o:calls ?b ."), "func_a o:calls func_b missing"
    assert _ask(og, "?a a o:Function ; rdfs:label 'func_a' ."
                     " ?w a o:Class ; rdfs:label 'Widget' ."
                     " ?a o:imports ?w ."), "func_a o:imports Widget missing"
    assert _ask(og, "?w a o:Class ; rdfs:label 'Widget' ."
                     " ?m a o:Method ; rdfs:label 'build' ."
                     " ?w o:method ?m ."), "Widget o:method build missing"
    assert _ask(og, "?r a o:Rationale ."
                     " ?a a o:Function ; rdfs:label 'func_a' ."
                     " ?r o:rationale_for ?a ."), "rationale o:rationale_for func_a missing"

    # The bundle entity never became a node, so it can't be an edge endpoint.
    assert not _ask(og, "?x rdfs:label 'bundle_fn' .")


def test_rationale_text_lands_as_a_comment_not_a_label(project):
    """A rationale row's `name` IS the extracted comment text -- it reads
    as rdfs:comment (prose), never as the primary rdfs:label."""
    from prism_service.services.ontology_graph import OntologyGraph, NS

    og = OntologyGraph(project)
    og.rebuild()

    q = (f"PREFIX o: <{NS}> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
         f"SELECT ?c WHERE {{ GRAPH ?g {{ ?r a o:Rationale ; rdfs:comment ?c }} }}")
    out = og.query(q)
    assert out["bindings"]
    assert out["bindings"][0]["c"] == "explains why func_a exists"


def test_symbols_carry_infile(project):
    from prism_service.services.ontology_graph import OntologyGraph, NS

    og = OntologyGraph(project)
    og.rebuild()

    q = (f"PREFIX o: <{NS}> "
         f"PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
         f"SELECT ?f WHERE "
         f"{{ GRAPH ?g {{ ?s a o:Function ; rdfs:label 'func_a' ; o:inFile ?f }} }}")
    out = og.query(q)
    assert out["bindings"]
    assert out["bindings"][0]["f"] == "services/foo.py"


def test_structure_rolls_up_code_kinds_correctly(project):
    from prism_service.services.ontology_graph import OntologyGraph

    og = OntologyGraph(project)
    og.rebuild()
    structure = og.structure()

    by_id = {c["id"]: c for c in structure["classes"]}
    assert by_id["Function"]["own_count"] == 2
    assert by_id["Class"]["own_count"] == 1
    assert by_id["Method"]["own_count"] == 1
    assert by_id["Rationale"]["own_count"] == 1
    # o:Code rolls up Function+Class+Method+Rationale+Module+Interface+Variable.
    # 6 since task 2bfe49db seeded a module row (models/x.py) too.
    assert by_id["Code"]["count"] == 6


def test_a_second_rebuild_does_not_double_the_triples(project, monkeypatch):
    """remove_graph + bulk_load, never an incremental add -- rebuilding
    twice on unchanged rows must not double the count of edges or nodes."""
    from prism_service.services.ontology_graph import OntologyGraph

    og = OntologyGraph(project)
    # task b1971944 made validate() post one "ontology" Queue signal per
    # firing rule, and each signal projects as a QueueItem on the NEXT
    # rebuild, so with the listener live the triple count moves once
    # before it settles. That convergence is pinned in
    # test_firing_rules_become_decisions; THIS test pins that rebuild
    # itself is REPLACE-not-append, so it runs with the listener silent
    # (a warm-up loop of rebuild+validate+subprocess took >30 s under the
    # full suite and tripped the faulthandler dump; waves 34/36/37).
    from prism_service.services import ontology_rules
    monkeypatch.setattr(ontology_rules, "_ON_VALIDATED", [])

    first = og.rebuild()
    second = og.rebuild()

    assert first["total_triples"] == second["total_triples"]
    assert first["per_class"] == second["per_class"]
    assert og._count("Function") == 2


# ----------------------------------------------------------------------
# Validation after a rebuild (task f9e0745e): a small graph validates
# inline, a large one schedules one background validation and coalesces
# rebuilds that land while it runs.
# ----------------------------------------------------------------------


def test_small_graph_validates_inline(monkeypatch):
    from prism_service.services import ontology_rules as r

    calls = []
    monkeypatch.setattr(r, "validate", lambda project: calls.append(project))
    assert r.validate_after_rebuild("p-small", 10) == "inline"
    assert calls == ["p-small"]


def test_large_graph_validates_on_a_single_flight_thread(monkeypatch):
    import threading, time
    from prism_service.services import ontology_rules as r

    started = threading.Event(); release = threading.Event(); calls = []

    def slow_validate(project):
        calls.append(project); started.set(); release.wait(5)

    monkeypatch.setattr(r, "validate", slow_validate)
    monkeypatch.setattr(r, "ASYNC_VALIDATE_TRIPLES", 100)
    assert r.validate_after_rebuild("p-big", 1000) == "scheduled"
    assert started.wait(5)
    assert r.validation_in_flight("p-big")
    # a second rebuild while running coalesces into one more run
    assert r.validate_after_rebuild("p-big", 1000) == "queued"
    assert r.validate_after_rebuild("p-big", 1000) == "queued"
    release.set()
    for _ in range(100):
        if not r.validation_in_flight("p-big") and len(calls) >= 2:
            break
        time.sleep(0.05)
    assert calls == ["p-big", "p-big"]
    assert not r.validation_in_flight("p-big")


def test_a_module_is_labelled_by_its_package_relative_path(project):
    """task 2bfe49db: the promoted-rule SPARQL filters on
    STRSTARTS(?fromPath, "models"); the full projection and the diff check
    (law_check._label_for) must agree on that label or the rule fires at
    the gate and stays quiet on the Ontology page."""
    from prism_service.services.ontology_graph import OntologyGraph

    og = OntologyGraph(project)
    og.rebuild()
    assert _ask(og, "?m a o:Module ; rdfs:label 'models/x.py' .")
    assert not _ask(og, "?m a o:Module ; rdfs:label 'x.py' .")
