"""task 6e858c89: the memory projection reads EVERY evidence shape PRISM
writes, so decision-has-evidence fires only on a decision with no evidence.
Measured on the prism project 2026-08-27: 74 of 82 violations were memories
whose evidence sat under "source_file", "tasks", "files" or "task_id"."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

SHAPES = {
    "alpha": lambda tid: {"task": tid},
    "beta": lambda tid: {"task_id": tid},
    "gamma": lambda tid: {"tasks": [tid]},
    "delta": lambda tid: {"file_paths": ["services/a.py"]},
    "epsilon": lambda tid: {"files": ["services/b.py"]},
    "zeta": lambda tid: {"source_file": "docs/c.md"},
}


DESCRIPTIONS = {
    "alpha": "The login page shows one banner after the first key.",
    "beta": "The gate card renders the rubric verdict in a muted row.",
    "gamma": "The queue sorts signals by arrival and hides dropped rows.",
    "delta": "The sidebar collapses on narrow screens under 900 pixels.",
    "epsilon": "The graph viewer pins the selected node at the centre.",
    "zeta": "The lexicon loads its synonym table once per process.",
}


@pytest.fixture
def project():
    from prism_service.project_context import get_project

    pid = f"evidence-shapes-{uuid.uuid4().hex[:8]}"
    ctx = get_project(pid)
    task = ctx.task_svc.create(title="the evidence task")
    for name, shape in SHAPES.items():
        ctx.memory_svc.store(
            domain="architecture", name=f"decision {name}",
            description=DESCRIPTIONS[name],
            type="decision", classification="tactical",
            evidence=shape(task.id),
        )
    ctx.memory_svc.store(
        domain="architecture", name="decision without evidence",
        description="A decision with no evidence at all.",
        type="decision", classification="tactical",
    )
    return pid, task.id


def test_every_shape_yields_evidence_rows(project):
    from prism_service.services.ontology_memory_projection import memory_rows

    pid, tid = project
    rows = {r["name"]: r for r in memory_rows(pid) if r["name"].startswith("decision")}
    assert rows["decision alpha"]["evidence_tasks"] == [tid]
    assert rows["decision beta"]["evidence_tasks"] == [tid]
    assert rows["decision gamma"]["evidence_tasks"] == [tid]
    assert rows["decision delta"]["evidence_files"] == ["services/a.py"]
    assert rows["decision epsilon"]["evidence_files"] == ["services/b.py"]
    assert rows["decision zeta"]["evidence_files"] == ["docs/c.md"]
    assert rows["decision without evidence"]["evidence_tasks"] == []
    assert rows["decision without evidence"]["evidence_files"] == []


def test_only_the_decision_without_evidence_violates(project):
    from prism_service.services.ontology_graph import OntologyGraph, NS

    pid, _ = project
    og = OntologyGraph(pid)
    og.rebuild()
    q = (f"PREFIX o: <{NS}> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
         f"SELECT ?l WHERE {{ GRAPH ?g {{ ?d a o:Decision ; rdfs:label ?l . "
         f"FILTER NOT EXISTS {{ ?d o:evidencedBy ?e }} }} }}")
    labels = sorted(b["l"] for b in og.query(q)["bindings"])
    assert labels == ["decision without evidence"]
