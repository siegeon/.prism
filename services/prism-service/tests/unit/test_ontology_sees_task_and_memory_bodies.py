"""The ontology sees what a task and a memory SAY, not only their names
(epic b2acfa16, owner 2026-08-26: "so are you saying that the ontology has
nothing to do with understand?").

Before this slice the projection emitted only rdfs:label (a task title, a
memory name). A rule that reads text, such as text-is-plain (task
5ac5d04c), was blind to the body people actually write. Now:
  1. task.description and memory.description ride into the graph as
     rdfs:comment on the o:Task / o:Concept node.
  2. text-is-plain fires on a body that breaks the rule while the title
     is clean.
  3. rule_catalog() lists a rule ONCE with every target class on that
     row, and looked_at_counts unions the instances of all its classes.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import rdflib

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_RDFS = rdflib.RDFS


@pytest.fixture
def project(tmp_path, monkeypatch):
    from prism_service import config as cfg
    from prism_service import project_context as pc

    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield f"bodies-{uuid.uuid4().hex[:8]}"
    pc._contexts.clear()


# A body the normaliser leaves alone (a quoted string is a protected
# span) but the ontology's regex still sees: the residual case.
_BODY = 'The owner called the first draft "robust" and asked for a plain rewrite.'


def _seed(project: str):
    from prism_service.project_context import get_project

    ctx = get_project(project)
    task = ctx.task_svc.create(title="A clean title", description=_BODY)
    memory = ctx.memory_svc.store(
        domain="conventions", name="a-clean-name", description=_BODY,
        type="decision", classification="tactical",
    )
    return ctx, task, memory


def test_task_and_memory_bodies_are_rdfs_comment_in_the_graph(project):
    from prism_service.services.ontology_graph import OntologyGraph, _iri

    ctx, task, memory = _seed(project)
    OntologyGraph(project).rebuild()
    g = OntologyGraph(project).to_rdflib()

    task_body = g.value(rdflib.URIRef(_iri("task", task.id)), _RDFS.comment)
    mem_body = g.value(rdflib.URIRef(_iri("memory", memory.id)), _RDFS.comment)
    assert task_body is not None and str(task_body) == task.description
    assert mem_body is not None and str(mem_body) == memory.description
    assert "robust" in str(task_body)  # the protected span survived the write


def test_text_is_plain_fires_on_a_body_when_the_title_is_clean(project):
    from prism_service.services import ontology_rules
    from prism_service.services.ontology_graph import OntologyGraph, _iri

    ctx, task, memory = _seed(project)
    OntologyGraph(project).rebuild()
    _inferred, violations = ontology_rules.run_shapes(OntologyGraph(project).to_rdflib())

    focus = set(violations.get("text-is-plain", []))
    assert _iri("task", task.id) in focus, violations
    assert _iri("memory", memory.id) in focus, violations


def test_rule_catalog_lists_a_shared_rule_once_with_all_its_targets():
    from prism_service.services import ontology_rules

    rows = [r for r in ontology_rules.rule_catalog() if r["name"] == "text-is-plain"]
    assert len(rows) == 1, rows
    # task ed034701 added o:Signal as a fifth target -- a signal's
    # aligned body is free text too (services/ontology_graph.py
    # _emit_signals now projects it as rdfs:comment).
    assert set(rows[0]["target_classes"]) == {
        "urn:prism:onto:Task", "urn:prism:onto:Decision",
        "urn:prism:onto:Term", "urn:prism:onto:Agent",
        "urn:prism:onto:Signal",
    }
    names = [r["name"] for r in ontology_rules.rule_catalog()]
    assert len(names) == len(set(names)), "a rule appears more than once"


def test_looked_at_unions_every_target_class_of_a_shared_rule(project):
    from prism_service.services import ontology_rules
    from prism_service.services.ontology_graph import OntologyGraph

    _seed(project)
    OntologyGraph(project).rebuild()
    g = OntologyGraph(project).to_rdflib()
    counts = ontology_rules.looked_at_counts(g, ontology_rules.rule_catalog())
    # One o:Task + one o:Decision at least: the union must be larger than
    # the Task count alone (the bug was counting one class only).
    tasks_only = ontology_rules.looked_at_counts(
        g, [{"name": "t", "target_class": "urn:prism:onto:Task"}])["t"]
    assert tasks_only >= 1
    assert counts["text-is-plain"] > tasks_only
