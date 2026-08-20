"""Task f506ece4: GET /api/workflows is the SINGLE source of the conductor
step list, the bot roster, and per-project live occupancy.

HARD CONSTRAINT from the owner: NO NEW ENTITIES. This endpoint mints
nothing — every field is read straight off an existing source of truth:

  steps      <- models/workflow.py WORKFLOW_STEPS (the conductor FSM)
  persona    <- models/roles.py STEP_ROLES (gates are Steward-adjudicated,
                which is why a gate's persona is "sm" even though the FSM
                row carries agent=None)
  bots       <- services/context_builder.py ROLE_CARDS (the role briefs)
  occupancy  <- the project's EXISTING task rows (task.workflow_step)

The endpoint existing is what lets lib/workflowChips.ts stop carrying a
hand-maintained duplicate of WORKFLOW_STEPS (pinned in
test_workflows_section_ui.py). If this contract drifts, that duplication
comes back — so the ordering assertion below is cross-checked against the
backend list itself rather than a literal copy pasted into this file.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest


def _mk_task(**over):
    from prism_service.models.task import Task

    base = dict(
        id="t-1", title="A task", description="", status="pending",
        priority=5, assigned_agent="", updated_at="2026-08-18T00:00:00Z",
        workflow_step="", gate_state="none", parent_id="", tags=[],
    )
    base.update(over)
    return Task(**base)


class _Svc:
    """Minimal stand-in for task_svc — the endpoint only ever LISTS."""

    def __init__(self, tasks):
        self.tasks = list(tasks)

    def list(self, status=None, assigned_agent=None, tag=None,
             story_file=None, parent_id=None, id=None):
        return list(self.tasks)


def _scripted_validation(project="prism"):
    return {
        "id": "validation", "name": "Build and test",
        "description": f"{project} validation", "project_type": "python+react",
        "steps": [
            {"id": "test", "agent": "qa", "type": "agent",
             "validation": "exit_code == 0", "persona": "qa",
             "persona_label": "Verifier", "purpose": "Run tests",
             "input": "source", "action": "uv run pytest -q",
             "output": "result", "execution": "scripted", "runner": "process",
             "command": "uv run pytest -q", "working_directory": "/repo/service",
             "timeout_seconds": 900, "depends_on": []},
            {"id": "build", "agent": "dev", "type": "agent",
             "validation": "exit_code == 0", "persona": "dev",
             "persona_label": "Builder", "purpose": "Build web",
             "input": "source", "action": "npm run build",
             "output": "result", "execution": "scripted", "runner": "process",
             "command": "npm run build", "working_directory": "/repo/web",
             "timeout_seconds": 300, "depends_on": ["test"]},
        ],
        "bots": [], "occupancy": {"test": 0, "build": 0},
    }


def _client(svc, monkeypatch, data_dir):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import workflows as workflows_api

    monkeypatch.setenv("PRISM_DATA_DIR", str(data_dir))
    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=svc))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)
    app = FastAPI()
    app.include_router(workflows_api.router, prefix="/api/workflows")
    return TestClient(app)


def _backend_step_ids():
    from prism_service.models.workflow import WORKFLOW_STEPS

    return [s["id"] for s in WORKFLOW_STEPS]


def test_endpoint_is_mounted_on_the_real_api_router():
    """The SPA reaches this at /api/workflows — a router nobody included is
    a 404 the page can only discover in production."""
    from fastapi import FastAPI
    from prism_service.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    assert "/api/workflows" in app.openapi()["paths"], (
        "GET /api/workflows is not mounted on api_router — the Workflows "
        "section would render an empty canvas against a 404")


def test_steps_are_the_backend_workflow_in_order(tmp_path, monkeypatch):
    """The FSM, verbatim and IN ORDER. The whole point of the endpoint is
    that the frontend never has to keep its own copy in sync."""
    client = _client(_Svc([]), monkeypatch, tmp_path / "data")
    body = client.get("/api/workflows?project=prism").json()

    ids = [s["id"] for s in body["steps"]]
    assert ids == _backend_step_ids(), (
        "served steps must be WORKFLOW_STEPS in FSM order — any reordering "
        "or omission silently rewrites the conductor rail in the SPA")
    assert len(ids) == 10, f"expected the 10-step conductor FSM, got {ids}"


def test_each_step_carries_the_fields_the_rail_renders(tmp_path, monkeypatch):
    """StepRail/SdlcProgress key off `type` (gate vs agent) and `persona`
    (who owns the row). Both must arrive from the API, not be re-derived."""
    client = _client(_Svc([]), monkeypatch, tmp_path / "data")
    steps = client.get("/api/workflows?project=prism").json()["steps"]

    for s in steps:
        for field in ("id", "agent", "type", "validation", "persona",
                      "persona_label", "purpose", "input", "action",
                      "output", "execution"):
            assert field in s, f"step {s.get('id')!r} is missing {field!r}: {s}"
        assert s["type"] in ("agent", "gate"), (
            f"step {s['id']} has an unknown type {s['type']!r}")


def test_catalog_exposes_conductor_and_build_test_validation(tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc([])))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)
    body = workflows_api.get_workflows("prism")

    assert [workflow["id"] for workflow in body["workflows"]] == [
        "conductor", "validation"]
    validation = body["workflows"][1]
    assert validation["name"] == "Build and test"
    assert [step["id"] for step in validation["steps"]] == ["test", "build"]
    assert [step["persona_label"] for step in validation["steps"]] == [
        "Verifier", "Builder"]
    assert validation["occupancy"] == {"test": 0, "build": 0}
    assert all(step["execution"] == "scripted"
               for step in validation["steps"])
    assert [step["command"] for step in validation["steps"]] == [
        "uv run pytest -q", "npm run build"]
    assert validation["steps"][1]["depends_on"] == ["test"]


def test_scripted_workflow_is_validated_from_the_aos_engine(monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "_workflow_engine_json", lambda path, method="GET": {
        "id": "validation", "name": "Build and test",
        "description": "real project validation", "project": "prism",
        "projectType": "python+react",
        "steps": [
            {"id": "test", "title": "Test", "purpose": "Run Python tests",
             "runner": "process", "command": "uv run pytest -q",
             "workingDirectory": "/home/user/projects/prism/services/prism-service",
             "timeoutSeconds": 900, "dependsOn": [], "success": "exit_code == 0"},
            {"id": "build", "title": "Build", "purpose": "Build React",
             "runner": "process", "command": "npm run build",
             "workingDirectory": "/home/user/projects/prism/services/prism-service/prism_service/web",
             "timeoutSeconds": 300, "dependsOn": ["test"], "success": "exit_code == 0"},
        ],
    })

    workflow = workflows_api._project_validation_workflow("prism")

    assert workflow["project_type"] == "python+react"
    assert [step["command"] for step in workflow["steps"]] == [
        "uv run pytest -q", "npm run build"]
    assert workflow["steps"][1]["depends_on"] == ["test"]
    assert all(step["execution"] == "scripted" for step in workflow["steps"])


def test_gates_are_owned_by_the_steward_not_left_blank(tmp_path, monkeypatch):
    """A gate row's FSM `agent` is None (nobody AUTHORS a gate), but the
    Steward ADJUDICATES it as the independent reviewer (models/roles.py
    STEP_ROLES). The rail must be able to name that actor, so `persona`
    resolves through STEP_ROLES rather than copying `agent`."""
    client = _client(_Svc([]), monkeypatch, tmp_path / "data")
    steps = client.get("/api/workflows?project=prism").json()["steps"]
    gates = [s for s in steps if s["type"] == "gate"]

    assert gates, "the FSM has gates; none were served"
    for g in gates:
        assert g["agent"] is None, (
            f"gate {g['id']} must report the FSM's own agent=None")
        assert g["persona"] == "sm", (
            f"gate {g['id']} must be Steward-adjudicated, got {g['persona']!r}")
        assert g["persona_label"] == "Steward", (
            f"gate {g['id']} label drifted from the role registry: {g}")


def test_the_four_bots_are_served_with_their_role_cards(tmp_path, monkeypatch):
    """A workflow IS a bot in PRISM. The canvas draws four actors, each
    named and briefed off the EXISTING ROLE_CARDS — no new roster."""
    from prism_service.services.context_builder import ROLE_CARDS

    client = _client(_Svc([]), monkeypatch, tmp_path / "data")
    bots = client.get("/api/workflows?project=prism").json()["bots"]

    assert [b["id"] for b in bots] == ["sm", "qa", "dev", "architect"]
    labels = {b["id"]: b["persona_label"] for b in bots}
    assert labels == {"sm": "Steward", "qa": "Verifier", "dev": "Builder",
                      "architect": "Architect"}, (
        f"bot labels must match the SPA's persona vocabulary: {labels}")
    for b in bots:
        assert b["card"] == ROLE_CARDS[b["id"]], (
            f"bot {b['id']}'s card must BE the existing ROLE_CARDS entry, "
            "not a paraphrase that can drift")


def test_occupancy_counts_live_tasks_per_step(tmp_path, monkeypatch):
    """Occupancy is a projection of task rows that already exist — it is
    what makes the canvas show the CURRENT board, not a diagram."""
    svc = _Svc([
        _mk_task(id="t-1", workflow_step="implement_tasks", status="in_progress"),
        _mk_task(id="t-2", workflow_step="implement_tasks", status="pending"),
        _mk_task(id="t-3", workflow_step="story_gate", status="blocked"),
    ])
    client = _client(svc, monkeypatch, tmp_path / "data")
    occ = client.get("/api/workflows?project=prism").json()["occupancy"]

    assert occ["implement_tasks"] == 2, f"occupancy: {occ}"
    assert occ["story_gate"] == 1, f"occupancy: {occ}"


def test_occupancy_excludes_done_work_and_unknown_steps(tmp_path, monkeypatch):
    """A finished task is not standing at a step, and a task parked at a
    step id the FSM does not contain (legacy rows) must not invent a node
    the canvas has nowhere to draw."""
    svc = _Svc([
        _mk_task(id="t-1", workflow_step="implement_tasks", status="done"),
        _mk_task(id="t-2", workflow_step="", status="in_progress"),
        _mk_task(id="t-3", workflow_step="a_step_that_never_existed",
                 status="in_progress"),
    ])
    client = _client(svc, monkeypatch, tmp_path / "data")
    occ = client.get("/api/workflows?project=prism").json()["occupancy"]

    assert occ["implement_tasks"] == 0, (
        f"a done task must not occupy its last step: {occ}")
    assert "a_step_that_never_existed" not in occ, (
        f"occupancy must be keyed by the FSM's own steps only: {occ}")
    assert "" not in occ, f"the empty step is not a node: {occ}"


def test_occupancy_covers_every_step_so_the_canvas_can_key_off_it(
        tmp_path, monkeypatch):
    """Every FSM step gets a key (0 when idle) so the renderer reads a
    count directly instead of branching on presence."""
    client = _client(_Svc([]), monkeypatch, tmp_path / "data")
    occ = client.get("/api/workflows?project=prism").json()["occupancy"]

    assert sorted(occ) == sorted(_backend_step_ids())
    assert set(occ.values()) == {0}, f"an empty board must be all zeroes: {occ}"


@pytest.mark.parametrize("project", ["prism", "another-project"])
def test_the_view_is_project_scoped(project, tmp_path, monkeypatch):
    """Per-project section: the project selector reaches the endpoint."""
    seen = []

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import workflows as workflows_api

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))

    def _fake_get_project(p):
        seen.append(p)
        return types.SimpleNamespace(task_svc=_Svc([]))

    monkeypatch.setattr(workflows_api, "get_project", _fake_get_project)
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)
    app = FastAPI()
    app.include_router(workflows_api.router, prefix="/api/workflows")
    client = TestClient(app)

    assert client.get(f"/api/workflows?project={project}").status_code == 200
    assert seen == [project], (
        f"the endpoint must resolve the REQUESTED project, saw {seen}")


def test_completed_run_history_is_proxied_from_the_workflow_engine(monkeypatch):
    from prism_service.api import workflows as workflows_api

    seen = []
    expected = {"runs": [{"id": "run-1", "status": "Complete"}]}

    def engine(path, method="GET"):
        seen.append((path, method))
        return expected

    monkeypatch.setattr(workflows_api, "_workflow_engine_json", engine)

    assert workflows_api.get_workflow_run_history("validation", "prism", 12) == expected
    assert seen == [("/workflows/history/prism?limit=12", "GET")]


def test_failed_step_queues_a_governed_agent_task(monkeypatch):
    from prism_service.api import workflows as workflows_api

    created = []

    class CreatingSvc(_Svc):
        def create(self, **fields):
            created.append(fields)
            return types.SimpleNamespace(id="fix-123", status="pending")

    def engine(path, method="GET"):
        if path == "/workflows/instances/run-1":
            return {
                "data": {
                    "project": "prism",
                    "sourceSnapshot": {
                        "schemaVersion": 1,
                        "repositoryRoot": str(Path.home() / "projects" / "prism"),
                        "baseCommit": "a" * 40,
                        "snapshotCommit": "b" * 40,
                        "tree": "c" * 40,
                        "dirty": True,
                        "includedUntracked": 1,
                    },
                    "tests": {
                        "status": "failed", "exitCode": 1,
                        "output": (
                            "FAILED tests/unit/test_x.py::test_contract - AssertionError\n"
                            "================ 1 failed in 0.12s ================\n"
                        ),
                    },
                },
            }
        assert path == "/workflows/definitions/prism"
        return {
            "id": "validation", "name": "Build and test",
            "description": "PRISM validation", "project": "prism",
            "projectType": "python+react",
            "steps": [{
                "id": "test", "title": "Test", "purpose": "Run tests",
                "runner": "process", "command": "bash scripts/workflows/test.sh",
                "workingDirectory": "/repo", "timeoutSeconds": 900,
                "dependsOn": [], "success": "exit_code == 0",
                "scriptPath": "/repo/scripts/workflows/test.sh",
            }],
        }

    monkeypatch.setattr(workflows_api, "_workflow_engine_json", engine)
    monkeypatch.setattr(
        workflows_api, "get_project",
        lambda project: types.SimpleNamespace(task_svc=CreatingSvc([])),
    )
    from prism_service.services import source_snapshot, task_workspace
    monkeypatch.setattr(source_snapshot, "validate_source_snapshot", lambda *args: None)
    monkeypatch.setattr(
        task_workspace, "ensure_workspace",
        lambda *args, **kwargs: {"baseline": "b" * 40, "path": "/workspace"},
    )

    result = workflows_api.queue_workflow_fix(
        "prism", "validation",
        workflows_api.WorkflowFixRequest(instance_id="run-1", step_id="test"),
    )

    assert result["task_id"] == "fix-123"
    assert created[0]["tags"] == ["workflow-fix", "agent-managed", "test"]
    assert created[0]["verify"] == ["bash scripts/workflows/test.sh"]
    assert result["validation"]["kind"] == "conductor.step_validation"
    assert result["validation"]["summary"] == "1 failed in 0.12s"
    assert result["validation"]["failures"] == [{
        "check": "tests/unit/test_x.py::test_contract",
        "message": "AssertionError",
    }]
    assert result["workspace"]["baseline"] == "b" * 40
    assert result["source_snapshot"]["snapshotCommit"] == "b" * 40
    assert "FAILED tests/" not in created[0]["description"]


def test_workflow_fix_intent_rejects_caller_supplied_execution_data():
    from pydantic import ValidationError
    from prism_service.api.workflows import WorkflowFixRequest

    with pytest.raises(ValidationError):
        WorkflowFixRequest.model_validate({
            "instance_id": "run-1", "step_id": "test",
            "command": "rm -rf /", "output": "pretend success",
        })


def test_provided_child_behavior_versions_parent_and_preserves_sibling(tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api

    definition = {
        "id": "validation", "name": "Build and test", "description": "x",
        "project": "prism", "projectType": "python+react", "behaviorVersion": 3,
        "steps": [
            {"id": "build", "title": "Build", "purpose": "build", "runner": "process",
             "command": "build", "workingDirectory": "/repo", "timeoutSeconds": 10,
             "dependsOn": [], "success": "exit_code == 0", "behaviorVersion": 1},
            {"id": "test", "title": "Test", "purpose": "test", "runner": "process",
             "command": "test", "workingDirectory": "/repo", "timeoutSeconds": 20,
             "dependsOn": ["build"], "success": "exit_code == 0", "behaviorVersion": 3},
        ],
    }
    monkeypatch.setattr(workflows_api, "_workflow_engine_json", lambda path: definition)
    target = tmp_path / ".prism" / "behaviors" / "validation.json"
    monkeypatch.setattr(workflows_api, "_behavior_file", lambda project, workflow: target)

    result = workflows_api.provide_workflow_behavior(
        "prism", "validation/test", 3, {"timeoutSeconds": 30})

    stored = workflows_api.json.loads(target.read_text())
    assert result == {"ok": True, "path": "validation/test", "version": 4,
                      "parentVersion": 4,
                      "historyReset": ["validation/test", "validation"],
                      "preservedSiblingHistory": ["build"]}
    assert stored["behaviorVersion"] == 4
    assert stored["steps"][0]["behaviorVersion"] == 1
    assert stored["steps"][1]["behaviorVersion"] == 4
    assert stored["steps"][1]["timeoutSeconds"] == 30

    with pytest.raises(Exception) as stale:
        workflows_api.provide_workflow_behavior(
            "prism", "validation/test", 2, {"timeoutSeconds": 40})
    assert getattr(stale.value, "status_code", None) == 409
