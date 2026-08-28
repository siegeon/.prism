"""Task 408138e8 (epic 61821448): every workflow and step description
says when it runs.

The ontology rule skill-description-says-when (ontology/shapes.ttl)
flags any o:Agent instance whose rdfs:comment lacks when/trigger
wording. o:Agent instances are projected from the /api/workflows
catalog (ontology_prototype_projection._agent_instances/
_catalog_entries; services/ontology_graph.py's _agent_descriptions
reads the SAME catalog's own `description` field as the rdfs:comment
the rule checks).

This test parses the rule's own REGEX(...) straight out of shapes.ttl
(so it can never silently drift from the real rule), then checks every
description the catalog can actually produce:

  1. The 10 implement-workflow steps' STEP_ACTIONS text -- the
     network-free fallback _catalog_entries falls back to when no
     AosWorkflows engine is reachable, which is exactly what happens in
     this test run (nothing listens on AOS_WORKFLOWS_URL in CI) -- so
     this IS what the scratch-project rebuild below actually projects.
  2. GET /api/workflows's full catalog (the 6 root workflows, plus
     "validation" and every conductor behavior) through a fake
     AosWorkflows engine -- never through the neighbouring
     test_api_workflows.py's `_client` fixture, which stubs
     _project_validation_workflow/_conductor_behavior_workflows out
     entirely and would bypass the very fix this task made.
  3. A full ontology rebuild for a scratch project reports zero real
     skill-description-says-when violations.
"""

from __future__ import annotations

import re
import types
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_SHAPES_TTL = _SERVICE_ROOT / "prism_service" / "ontology" / "shapes.ttl"


def _rule_regex() -> re.Pattern:
    """Pull skill-description-says-when's own REGEX(?c, "...", "...")
    call out of shapes.ttl at test time -- never a hand-copied pattern
    that can drift from the real rule."""
    text = _SHAPES_TTL.read_text()
    marker = "o:skill-description-says-when a sh:SPARQLConstraint"
    start = text.index(marker)
    end = text.find("\no:", start + len(marker))
    block = text[start:end if end != -1 else len(text)]
    m = re.search(r'REGEX\(\?c,\s*"([^"]+)"\s*,\s*"([^"]*)"\)', block)
    assert m, "could not find skill-description-says-when's REGEX(...) in shapes.ttl"
    pattern, flags = m.group(1), m.group(2)
    return re.compile(pattern, re.IGNORECASE if "i" in flags else 0)


class _Svc:
    """Minimal task_svc stand-in -- the endpoint only ever LISTS."""

    def __init__(self, tasks=()):
        self.tasks = list(tasks)

    def list(self, status=None, assigned_agent=None, tag=None,
             story_file=None, parent_id=None, id=None):
        return list(self.tasks)


def test_step_actions_say_when():
    """The network-free fallback _catalog_entries reads STEP_ACTIONS'
    own action text for every WORKFLOW_STEPS id -- exactly what the
    scratch-project ontology rebuild below actually projects, since no
    AosWorkflows engine is reachable in this test run."""
    from prism_service.api.workflows import STEP_ACTIONS
    from prism_service.models.workflow import WORKFLOW_STEPS

    rx = _rule_regex()
    for step in WORKFLOW_STEPS:
        sid = step["id"]
        action = STEP_ACTIONS[sid][1]
        assert rx.search(action), (
            f"{sid}'s STEP_ACTIONS text has no when/trigger: {action!r}")


def _fake_engine_factory(behavior_ids):
    def _fake_engine(path: str, method: str = "GET", body: dict | None = None) -> dict:
        if path.startswith("/workflows/definitions/"):
            return {
                "id": "validation", "name": "Build and test",
                "description": "prism validation", "project": "prism",
                "projectType": "python+react", "steps": [],
            }
        if path.startswith("/workflows/bots/conductor/behaviors/"):
            behavior_id = path.split(
                "/workflows/bots/conductor/behaviors/", 1)[1].split("?", 1)[0]
            return {"id": behavior_id, "fsmId": "pipeline", "botId": "conductor",
                    "name": behavior_id.replace("-", " ").title(),
                    "version": 1, "steps": []}
        if path.startswith("/workflows/bots/conductor?"):
            return {"id": "conductor", "name": "Conductor",
                    "fsms": [{"fsmId": "pipeline", "behaviorIds": list(behavior_ids)}]}
        raise AssertionError(f"unexpected workflow engine path: {path}")
    return _fake_engine


def test_conductor_and_named_workflow_descriptions_say_when(tmp_path, monkeypatch):
    """GET /api/workflows through the REAL description-producing code --
    only the network boundary (_workflow_engine_json) and
    _project_source_path are faked, so _project_validation_workflow and
    _conductor_behavior_workflows run for real, exercising this task's
    actual fix."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc()))
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))
    monkeypatch.setattr(
        workflows_api, "_workflow_engine_json",
        _fake_engine_factory(workflows_api._BEHAVIOR_TRIGGER.keys()))

    app = FastAPI()
    app.include_router(workflows_api.router, prefix="/api/workflows")
    client = TestClient(app)

    body = client.get("/api/workflows?project=prism").json()
    rx = _rule_regex()

    ids = [w["id"] for w in body["workflows"]]
    assert len(ids) >= 16, f"expected at least the 16 known catalog entries, got {ids}"
    for entry in body["workflows"]:
        assert rx.search(entry["description"]), (
            f"{entry['id']}'s description has no when/trigger: "
            f"{entry['description']!r}")


def test_ontology_rebuild_reports_zero_skill_description_violations():
    """The real projection path, end to end: a fresh scratch project's
    o:Agent instances (network-free fallback -- exactly STEP_ACTIONS'
    10 ids, since no AosWorkflows engine is reachable here) must report
    zero skill-description-says-when violations after a real rebuild."""
    from prism_service.project_context import get_project
    from prism_service.services.ontology_graph import OntologyGraph

    pid = f"workflow-desc-says-when-{uuid.uuid4().hex[:8]}"
    get_project(pid)
    graph = OntologyGraph(pid)
    graph.rebuild()

    axioms = {a["name"]: a for a in graph.axioms()}
    entry = axioms.get("skill-description-says-when")
    assert entry is not None, (
        "skill-description-says-when missing from the rebuilt axiom report")
    assert entry["violations"] == 0, (
        f"skill-description-says-when fired on the rebuilt o:Agent set: {entry['detail']}")
