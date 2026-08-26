"""Understanding respects the ontology (task f5352fa1, epic 3a652b3b,
owner: "we need to make sure that the ontology is respected throughout the
system, understanding and such").

Pins the four parts of the description:

  1. Memory entries -> ontology triples: services.ontology_memory_projection
     .memory_rows feeds ontology_graph.OntologyGraph._emit_memories, which
     types entries by their memory `type` (o:Decision etc under o:Concept),
     with o:inDomain / o:cites / o:evidencedBy real edges.
  2. Understand respects it: GET /api/okf/ontology/concept serves class/
     domain/relations for one concept; UnderstandPage.tsx renders the 'In
     the ontology' strip bound to it (source-asserted -- no JS test runner).
  3. Search results carry the ontology: memory_recall / brain_search MCP
     results gain `ontology_class` resolved off the live graph.
  4. The vocabularies govern the APIs: POST/PATCH /api/tasks and MCP
     task_create/task_update refuse an undeclared status or proof_type.

Also pins the three new SHACL rules in shapes-knowledge.ttl: each gets a
compliant fixture (validates clean) and a violating one (fires, by IRI).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest
import rdflib

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_UNDERSTAND_PAGE = _SRC / "pages" / "UnderstandPage.tsx"
_MODEL_TTL = _SERVICE_ROOT / "prism_service" / "ontology" / "model.ttl"
_MODEL_KNOWLEDGE_TTL = (
    _SERVICE_ROOT / "prism_service" / "ontology" / "model-knowledge.ttl"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext (mirrors test_task_channel_provenance.py's
    `project` fixture) so the REST router, MCP handle_tool, and the ontology
    graph all resolve the SAME tmp-backed data dir."""
    from prism_service import config as cfg
    from prism_service import project_context as pc

    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield f"understand-ontology-{uuid.uuid4().hex[:8]}"
    pc._contexts.clear()


def _seed_memory(project: str):
    """A cited 'base' concept, then a decision entry that [[links]] to it
    and carries evidence (a real task + a file path) -- exactly the shape
    the description's part (1) asks the memory projection to turn into
    o:Decision / o:inDomain / o:cites / o:evidencedBy triples."""
    from prism_service.project_context import get_project

    ctx = get_project(project)
    task = ctx.task_svc.create(title="ship the ontology strip")

    base = ctx.memory_svc.store(
        domain="architecture", name="Base pattern for strips",
        description="A reusable shape for read-only info strips.",
        type="pattern", classification="tactical",
    )
    decision = ctx.memory_svc.store(
        domain="architecture", name="Understand shows the ontology strip",
        description=(
            "Per [[Base pattern for strips]], the concept read panel shows "
            "the ontology strip above the body."
        ),
        type="decision", classification="tactical",
        evidence={"task": task.id, "file_paths": ["services/x.py"]},
    )
    return ctx, task, base, decision


# ---------------------------------------------------------------------------
# (1) memory entries -> ontology triples: o:Decision with inDomain/cites/
#     evidencedBy, real edges never fabricated
# ---------------------------------------------------------------------------

def test_memory_entry_projects_as_a_decision_concept_in_the_graph(project):
    from prism_service.services.ontology_graph import OntologyGraph, _iri

    ctx, task, base, decision = _seed_memory(project)
    graph = OntologyGraph(project)
    graph.rebuild()

    dec_iri = _iri("memory", decision.id)
    base_iri = _iri("memory", base.id)
    task_iri = _iri("task", task.id)

    ask = f"""
        PREFIX o: <urn:prism:onto:>
        ASK {{ GRAPH ?g {{
            <{dec_iri}> a o:Decision ;
                o:inDomain ?d ;
                o:cites <{base_iri}> ;
                o:evidencedBy <{task_iri}> .
            ?d a o:Domain .
        }} }}
    """
    result = graph.query(ask)
    assert result["bindings"][0]["ask"] == "true", result

    # The cited concept itself is also a real, typed o:Concept subclass.
    ask_base = (
        f"PREFIX o: <urn:prism:onto:> ASK {{ GRAPH ?g {{ "
        f"<{base_iri}> a o:Pattern }} }}"
    )
    assert graph.query(ask_base)["bindings"][0]["ask"] == "true"


def test_gather_carries_a_memories_key_the_graph_consumes(project):
    from prism_service.services import ontology_prototype_projection as proj

    _seed_memory(project)
    rows = proj.gather(project)
    assert "memories" in rows
    names = {m["name"] for m in rows["memories"]}
    assert "Understand shows the ontology strip" in names
    decision_row = next(
        m for m in rows["memories"]
        if m["name"] == "Understand shows the ontology strip"
    )
    assert decision_row["type"] == "decision"
    assert decision_row["domain"] == "architecture"
    assert decision_row["cites"], "the [[wikilink]] must resolve to a real id"
    assert decision_row["evidence_task"]


# ---------------------------------------------------------------------------
# (2) Understand respects it: GET /api/okf/ontology/concept + the rendered
#     'In the ontology' strip (source-asserted, no JS test runner)
# ---------------------------------------------------------------------------

def test_get_ontology_concept_returns_class_domain_and_relations(project):
    from prism_service.api import okf
    from prism_service.services.ontology_graph import OntologyGraph

    ctx, task, base, decision = _seed_memory(project)
    OntologyGraph(project).rebuild()

    okf._HOSTS.clear()
    info = okf.ontology_concept(project=project, id=decision.id)
    assert info["class"] == "Decision"
    assert info["domain"] == "architecture"
    assert [c["id"] for c in info["cites"]] == [base.id]
    assert [t["id"] for t in info["evidenced_by_tasks"]] == [task.id]
    assert info["evidenced_by_documents"] == []


def test_get_ontology_concept_is_empty_shaped_for_an_unknown_id(project):
    from prism_service.api import okf
    from prism_service.services.ontology_graph import OntologyGraph

    OntologyGraph(project).rebuild()
    okf._HOSTS.clear()
    info = okf.ontology_concept(project=project, id="mx-does-not-exist")
    assert info == {
        "class": "", "domain": "", "cites": [],
        "evidenced_by_tasks": [], "evidenced_by_documents": [],
    }


def test_understand_page_renders_the_in_the_ontology_strip():
    src = _UNDERSTAND_PAGE.read_text(encoding="utf-8")
    assert "In the ontology" in src
    assert "/api/okf/ontology/concept" in src
    assert "/ontology?tab=structure" in src
    # The strip is a real rendered component call, not just a comment.
    assert "<OntologyStrip" in src
    assert "function OntologyStrip" in src


# ---------------------------------------------------------------------------
# (3) Search results carry the ontology: memory_recall / brain_search gain
#     ontology_class, resolved off the live graph (empty when unknown)
# ---------------------------------------------------------------------------

def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def test_memory_recall_results_carry_ontology_class(project):
    from prism_service.services.ontology_graph import OntologyGraph

    ctx, task, base, decision = _seed_memory(project)
    OntologyGraph(project).rebuild()

    body = json.loads(_call(
        "memory_recall",
        {"query": "Understand shows the ontology strip", "domain": "architecture",
         "limit": 10},
        project,
    ))
    row = next(r for r in body if r["id"] == decision.id)
    assert row["ontology_class"] == "Decision"


def test_brain_search_results_carry_ontology_class_for_a_seeded_hit(project):
    from prism_service.services.ontology_graph import OntologyGraph

    ctx, task, base, decision = _seed_memory(project)
    OntologyGraph(project).rebuild()

    body = json.loads(_call(
        "brain_search",
        {"query": "Understand shows the ontology strip", "limit": 10},
        project,
    ))
    hits = body if isinstance(body, list) else body.get("results", body)
    matching = [
        r for r in hits
        if isinstance(r, dict)
        and str(r.get("doc_id", "")).startswith(f"memory/architecture/{decision.id}")
    ]
    assert matching, f"seeded memory entry not found among brain_search hits: {hits}"
    assert matching[0]["ontology_class"] == "Decision"


# ---------------------------------------------------------------------------
# (4) the vocabularies govern the APIs: an undeclared status/proof_type is
#     refused, naming the allowed values, on WRITE (REST + MCP)
# ---------------------------------------------------------------------------

def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_post_api_tasks_refuses_an_unknown_proof_type(project):
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "bogus proof", "proof_type": "vibes"})
    assert post.status_code == 400, post.text
    assert "proof_type" in post.text
    assert "vibes" in post.text


def test_patch_api_tasks_refuses_an_unknown_status(project):
    client = _api_client()
    post = client.post("/api/tasks", params={"project": project},
                       json={"title": "real task"})
    tid = post.json()["task"]["id"]
    patch = client.patch(f"/api/tasks/{tid}", params={"project": project},
                         json={"status": "urgent"})
    assert patch.status_code == 400, patch.text
    assert "status" in patch.text
    assert "urgent" in patch.text


def test_mcp_task_create_refuses_an_unknown_proof_type(project):
    body = json.loads(_call(
        "task_create", {"title": "bogus", "proof_type": "vibes"}, project))
    assert body["error"] == "proof_type_validation_failed"
    assert "vibes" in body["detail"]


def test_mcp_task_update_refuses_an_unknown_status(project):
    from prism_service.project_context import get_project

    ctx = get_project(project)
    t = ctx.task_svc.create(title="real task")
    body = json.loads(_call(
        "task_update", {"id": t.id, "status": "urgent"}, project))
    assert body["error"] == "status_validation_failed"
    assert "urgent" in body["detail"]


def test_status_and_proof_type_vocabularies_on_the_model():
    from prism_service.models import task as task_model

    assert task_model.validate_status("in_progress") == "in_progress"
    assert task_model.validate_status("") == ""
    with pytest.raises(ValueError):
        task_model.validate_status("urgent")

    assert task_model.validate_proof_type("test") == "test"
    assert task_model.validate_proof_type("") == ""
    with pytest.raises(ValueError):
        task_model.validate_proof_type("vibes")


# ---------------------------------------------------------------------------
# The three knowledge rules (shapes-knowledge.ttl): compliant fixture
# validates clean, violating fixture fires by IRI
# ---------------------------------------------------------------------------

_PREFIXES = """
@prefix o: <urn:prism:onto:> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""


def _graph(snippet: str) -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(str(_MODEL_TTL), format="turtle")
    g.parse(str(_MODEL_KNOWLEDGE_TTL), format="turtle")
    g.parse(data=snippet, format="turtle", publicID="urn:prism:onto:")
    return g


RULE_FIXTURES: dict[str, tuple[str, str, str]] = {
    "concept-names-its-domain": (
        _PREFIXES + "o:c1 a o:Concept ; o:inDomain o:d1 .",
        _PREFIXES + "o:c1 a o:Concept .",
        "c1",
    ),
    "concept-cites-only-known": (
        _PREFIXES + "o:c1 a o:Concept . o:c2 a o:Concept . o:c1 o:cites o:c2 .",
        _PREFIXES + "o:c1 a o:Concept ; o:cites o:missing .",
        "c1",
    ),
    "decision-has-evidence": (
        _PREFIXES + "o:dec1 a o:Decision ; o:evidencedBy o:t1 .",
        _PREFIXES + "o:dec1 a o:Decision .",
        "dec1",
    ),
}


def test_shapes_knowledge_ttl_declares_the_three_rules():
    from prism_service.services import ontology_rules

    catalog = {r["name"] for r in ontology_rules.rule_catalog()}
    assert set(RULE_FIXTURES) <= catalog


@pytest.mark.parametrize("rule_name", sorted(RULE_FIXTURES))
def test_knowledge_rule_is_quiet_on_compliant_and_fires_on_violation(rule_name):
    from prism_service.services import ontology_rules

    compliant_snippet, violating_snippet, focus_local = RULE_FIXTURES[rule_name]

    _inferred, quiet_violations = ontology_rules.run_shapes(_graph(compliant_snippet))
    assert rule_name not in quiet_violations, (
        f"{rule_name} fired on its OWN compliant fixture: "
        f"{quiet_violations.get(rule_name)}")

    _inferred, bad_violations = ontology_rules.run_shapes(_graph(violating_snippet))
    assert rule_name in bad_violations, (
        f"{rule_name} did not fire on its OWN violating fixture — "
        "a rule that cannot fail is decoration")
    focus_nodes = bad_violations[rule_name]
    assert any(f.endswith(focus_local) for f in focus_nodes), (
        rule_name, focus_nodes, focus_local)
