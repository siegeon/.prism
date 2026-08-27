"""The ontology is an RDF graph you can query with SPARQL (task 495d3a69,
epic 3efbcd89, owner: "it does not look like you used the ontology rules
and libraries we had in the subsume project").

rdflib emits classes+instances as RDF; pyoxigraph is the persisted store
holding ONE replaced named graph per project; SPARQL is how anything asks
the model. Seeds a throwaway project the same way
test_prototype_ontology_classes.py does (tasks with channels via
TaskService, a docs table, a graph.db entities/relationships table).
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_MODEL_TTL = _SERVICE_ROOT / "prism_service" / "ontology" / "model.ttl"


@pytest.fixture
def project():
    """A throwaway project seeded like test_prototype_ontology_classes.py's
    'project' fixture: tasks with channels via TaskService, a docs table
    (brain.db), and a code-graph entities/relationships table (graph.db)."""
    from prism_service.project_context import get_project

    pid = f"ontology-rdf-{uuid.uuid4().hex[:8]}"
    ctx = get_project(pid)
    ctx.task_svc.create(title="ui task", channel="ui")
    ctx.task_svc.create(title="mcp task", channel="mcp")
    ctx.task_svc.create(title="legacy task")  # blank channel

    from prism_service.config import project_data_dir

    brain_db = project_data_dir(pid) / "brain.db"
    conn = sqlite3.connect(str(brain_db))
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, source_file TEXT)")
    conn.execute("INSERT INTO docs VALUES ('d1','services/foo.py')")
    conn.execute("INSERT INTO docs VALUES ('d2','services/bar.py')")
    # Padding so a real rebuild's triple count genuinely clears the
    # oracle's ">100" bar (a real project's docs table dwarfs this) —
    # distinct paths/folders, not touching the ui/mcp/legacy task rows
    # the channel-behavior tests below assert on.
    for i in range(40):
        conn.execute(
            "INSERT INTO docs VALUES (?, ?)",
            (f"pad{i}", f"padding/folder{i % 5}/file{i}.py"),
        )
    conn.commit()
    conn.close()

    graph_db = project_data_dir(pid) / "graph.db"
    conn = sqlite3.connect(str(graph_db))
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT, kind TEXT)")
    conn.execute("CREATE TABLE relationships (relation TEXT)")
    conn.execute("INSERT INTO entities (name, kind) VALUES ('foo', 'function')")
    conn.execute("INSERT INTO entities (name, kind) VALUES ('Bar', 'class')")
    conn.execute("INSERT INTO entities (name, kind) VALUES ('baz', 'function')")
    conn.execute("INSERT INTO relationships VALUES ('calls')")
    conn.execute("INSERT INTO relationships VALUES ('imports')")
    conn.commit()
    conn.close()

    return pid


# ---------------------------------------------------------------------------
# model.ttl parses (rdflib) and declares the prototype classes as
# rdfs:Class with subclass edges
# ---------------------------------------------------------------------------

def test_model_ttl_parses_and_declares_classes_with_subclass_edges():
    import rdflib

    g = rdflib.Graph()
    g.parse(str(_MODEL_TTL), format="turtle")
    assert len(g) > 50

    NS = rdflib.Namespace("urn:prism:onto:")
    classes = set(g.subjects(rdflib.RDF.type, rdflib.RDFS.Class))
    for name in ("Task", "Channel", "Agent", "Provider", "Document", "Folder",
                 "Function", "Class", "Method", "Module"):
        assert NS[name] in classes, f"o:{name} not declared as rdfs:Class"

    # Real subclass edges, not a flat class list.
    assert (NS["Task"], rdflib.RDFS.subClassOf, NS["Activity"]) in g
    assert (NS["Person"], rdflib.RDFS.subClassOf, NS["Party"]) in g
    assert (NS["Function"], rdflib.RDFS.subClassOf, NS["Code"]) in g
    assert (NS["Signal"], rdflib.RDFS.subClassOf, NS["QueueItem"]) in g

# ---------------------------------------------------------------------------
# rebuild() writes real triples to the pyoxigraph store; a second rebuild
# REPLACES the named graph rather than appending to it
# ---------------------------------------------------------------------------

def test_rebuild_writes_triples_and_second_rebuild_replaces(project, monkeypatch):
    from prism_service.services.ontology_graph import OntologyGraph

    graph = OntologyGraph(project)
    assert graph.is_empty()

    result = graph.rebuild()
    assert result["total_triples"] > 100
    assert not graph.is_empty()

    count_q = "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }"

    # task b1971944: validate() posts Queue signals that project as
    # QueueItems on the next rebuild; that convergence is pinned in
    # test_firing_rules_become_decisions. This test pins REPLACE-not-append,
    # so the listener stays silent here (the warm-up loop it replaced took
    # >30 s under the full suite; waves 34/36/37).
    from prism_service.services import ontology_rules
    monkeypatch.setattr(ontology_rules, "_ON_VALIDATED", [])
    graph.rebuild()

    first_total = int(next(iter(graph.query(count_q)["bindings"]))["n"])

    graph.rebuild()
    second_total = int(next(iter(graph.query(count_q)["bindings"]))["n"])
    assert second_total == first_total, "a second rebuild must REPLACE, not append"

# ---------------------------------------------------------------------------
# SPARQL over the seeded ABox returns the real seeded tasks with channels
# ---------------------------------------------------------------------------

def test_sparql_returns_seeded_tasks_with_their_channels(project):
    from prism_service.services.ontology_graph import OntologyGraph

    graph = OntologyGraph(project)
    graph.rebuild()

    sparql = (
        "PREFIX o: <urn:prism:onto:> "
        "SELECT ?task ?channel WHERE { GRAPH ?g { "
        "?task a o:Task ; o:arrivedVia ?channel } }"
    )
    result = graph.query(sparql)
    assert result["columns"] == ["task", "channel"]
    assert not result["truncated"]

    channels = {row["channel"].rsplit("/", 1)[-1] for row in result["bindings"]}
    # the blank-channel "legacy task" never arrived via a channel, so it
    # correctly has no arrivedVia edge and is absent from these bindings.
    assert channels == {"ui", "mcp"}
    assert len(result["bindings"]) == 2

# ---------------------------------------------------------------------------
# GET /api/okf/ontology answers the same shape as before, sourced from the
# graph (counts, classes, properties, axioms all present and consistent)
# ---------------------------------------------------------------------------

def test_get_ontology_answers_same_shape_with_counts_from_the_graph(project):
    from prism_service.api import okf
    from prism_service.services.ontology_graph import OntologyGraph

    okf._HOSTS.clear()
    out = okf.ontology(project=project)  # empty graph -> auto-rebuilds
    class_ids = {c["id"] for c in out["classes"]}
    assert {"Channel", "Agent", "Provider", "Task", "Document", "Folder"} <= class_ids

    task_class = next(c for c in out["classes"] if c["id"] == "Task")
    assert task_class["instance_count"] == 3  # ui/mcp/legacy tasks

    graph = OntologyGraph(project)
    assert graph.classes() == out["classes"]

    inst = okf.ontology_instances(project=project, class_id="Task", limit=10)
    assert len(inst["instances"]) == 3
    assert {i["label"] for i in inst["instances"]} == {"ui task", "mcp task", "legacy task"}

    assert out["properties"]
    assert out["axioms"]

# ---------------------------------------------------------------------------
# The /sparql API route: real rows for SELECT, 400 for anything else
# ---------------------------------------------------------------------------

def test_post_sparql_route_answers_select_and_rejects_writes(project):
    from prism_service.api import okf
    from fastapi import HTTPException

    okf.ontology_rebuild(project=project)

    out = okf.ontology_sparql(
        {"query": "PREFIX o: <urn:prism:onto:> SELECT ?task WHERE "
                   "{ GRAPH ?g { ?task a o:Task } }"},
        project=project,
    )
    assert len(out["bindings"]) == 3
    assert "elapsed_ms" in out

    for bad in ("INSERT DATA { <urn:x> <urn:y> <urn:z> }",
                "DELETE WHERE { ?s ?p ?o }",
                "CONSTRUCT WHERE { ?s ?p ?o }"):
        with pytest.raises(HTTPException) as exc_info:
            okf.ontology_sparql({"query": bad}, project=project)
        assert exc_info.value.status_code == 400

# ---------------------------------------------------------------------------
# api/okf.py no longer imports OntologyStore for reads — grep-assert
# ---------------------------------------------------------------------------

def test_okf_api_no_longer_imports_ontology_store():
    okf_src = (_SERVICE_ROOT / "prism_service" / "api" / "okf.py").read_text(
        encoding="utf-8")
    assert "import ontology_store" not in okf_src
    assert "from prism_service.services.ontology_store" not in okf_src
    assert "OntologyStore(" not in okf_src
    assert "from prism_service.services.ontology_graph import OntologyGraph" in okf_src
