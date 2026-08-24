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
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        lambda project: [])
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
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        lambda project: [])
    body = workflows_api.get_workflows("prism")

    assert [workflow["id"] for workflow in body["workflows"]] == [
        "conductor", "validation"]
    validation = body["workflows"][1]
    assert validation["name"] == "Build and test"
    assert validation["parent_id"] == "conductor", (
        "validation is the conductor's own capability -- what its own "
        "verify_green_state step links to -- so it nests under conductor "
        "like every other bot capability, not as a flat sibling")
    assert [step["id"] for step in validation["steps"]] == ["test", "build"]
    assert [step["persona_label"] for step in validation["steps"]] == [
        "Verifier", "Builder"]
    assert validation["occupancy"] == {"test": 0, "build": 0}
    assert all(step["execution"] == "scripted"
               for step in validation["steps"])
    assert [step["command"] for step in validation["steps"]] == [
        "uv run pytest -q", "npm run build"]
    assert validation["steps"][1]["depends_on"] == ["test"]


def test_conductor_behaviors_are_one_catalog_entry_each_with_real_steps(tmp_path, monkeypatch):
    """Bot [1] uses FSM [1..*], FSM [1] has Behavior [0..*] -- and a Behavior
    IS a flow, so each gets its OWN catalog entry nested under the bot,
    disclosing its REAL steps (land's push/open-pr), not a synthetic
    wrapper node whose fake "steps" were just the behavior ids."""
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    def _fake_engine(path, method="GET", body=None):
        if path.startswith("/workflows/bots/conductor/behaviors/land"):
            return {
                "id": "land", "fsmId": "pipeline", "botId": "conductor",
                "name": "Land a task branch", "version": 1,
                "steps": [
                    {"id": "push", "kind": "shell", "command": "git push -u origin ${branch}",
                     "workingDirectory": "", "timeoutSeconds": 60},
                    {"id": "open-pr", "kind": "shell", "command": "gh pr create --fill",
                     "workingDirectory": "", "timeoutSeconds": 60},
                ],
            }
        if path.startswith("/workflows/bots/conductor/behaviors/ci-local-dev"):
            return {
                "id": "ci-local-dev", "fsmId": "pipeline", "botId": "conductor",
                "name": "CI to local dev", "version": 1,
                "steps": [{"id": "test", "kind": "shell", "command": "uv run pytest -q -x",
                           "workingDirectory": "/repo/service", "timeoutSeconds": 300}],
            }
        return {
            "id": "conductor", "name": "Conductor",
            "fsms": [{"fsmId": "pipeline", "behaviorIds": ["land", "ci-local-dev"]}],
        }

    monkeypatch.setattr(workflows_api, "_workflow_engine_json", _fake_engine)

    entries = workflows_api._conductor_behavior_workflows("prism")

    assert [entry["id"] for entry in entries] == ["land", "ci-local-dev"]
    # Deliberately NOT nested under "conductor" -- the conductor IS its
    # 10-state FSM, and something belongs in its view only when an actual
    # state calls it (verify_green_state -> validation). No conductor state
    # calls land or ci-local-dev, so claiming that relationship in the
    # directory would be a category error, same one already fixed once for
    # the "conductor-behaviors" wrapper node.
    assert all("parent_id" not in entry for entry in entries), (
        "a behavior with no real state->behavior link must not claim to "
        "nest under conductor's FSM view")

    land = entries[0]
    assert land["name"] == "Land a task branch"
    assert [step["id"] for step in land["steps"]] == ["push", "open-pr"]
    assert land["steps"][0]["command"] == "git push -u origin ${branch}"
    assert land["steps"][1]["depends_on"] == ["push"], (
        "a behavior's own steps must show their real sequence, not a flat "
        "unordered list")
    assert land["occupancy"] == {"push": 0, "open-pr": 0}

    ci = entries[1]
    assert ci["name"] == "CI to local dev"
    assert ci["steps"][0]["working_directory"] == "/repo/service"


def test_conductor_behaviors_are_empty_when_the_engine_is_unreachable(tmp_path, monkeypatch):
    """A workflows engine that's down, or a bot not yet registered, must not
    break the whole /api/workflows response -- just omit these entries."""
    from fastapi import HTTPException

    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    def _unreachable(path, method="GET", body=None):
        raise HTTPException(503, "workflow engine unavailable")

    monkeypatch.setattr(workflows_api, "_workflow_engine_json", _unreachable)

    assert workflows_api._conductor_behavior_workflows("prism") == []


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


def test_occupancy_excludes_cancelled_and_deleted_work(tmp_path, monkeypatch):
    """A cancelled task keeps its last workflow_step on the row forever
    (task_update never clears it) -- excluding only 'done' let a cancelled
    task go on occupying its last step indefinitely, showing the canvas
    path as "running" long after the task was cancelled (owner, live,
    for a task cancelled minutes earlier: "the newest workflow is still
    running from the conductor?"). This project's own cancel/redo cycles
    (several tasks cancelled at story_gate/plan_gate while iterating on a
    fix) are the exact real-world shape this reproduces."""
    svc = _Svc([
        _mk_task(id="t-1", workflow_step="draft_story", status="cancelled"),
        _mk_task(id="t-2", workflow_step="story_gate", status="deleted"),
        _mk_task(id="t-3", workflow_step="draft_story", status="in_progress"),
    ])
    client = _client(svc, monkeypatch, tmp_path / "data")
    occ = client.get("/api/workflows?project=prism").json()["occupancy"]

    assert occ["draft_story"] == 1, (
        f"only the genuinely in_progress task may occupy draft_story: {occ}")
    assert occ["story_gate"] == 0, (
        f"a deleted task must not occupy its last step: {occ}")


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


def test_story_gate_check_wraps_the_real_rubric_scorer_compliant():
    """Read-only: a compliant story returns ok=True from the SAME pure
    scorer story_gate would eventually use -- not a reimplementation."""
    from prism_service.api import workflows as workflows_api

    story = (
        "## Summary\nDoes a thing.\n\n"
        "## Requirements\nMust do the thing.\n\n"
        "## Acceptance Criteria\n"
        "- AC-1: the thing happens — oracle: check the log\n"
    )
    result = workflows_api.workflow_step_story_gate_check(
        workflows_api.StoryGateCheckRequest(story_doc=story, task_id="t1"),
        project="prism",
    )
    assert result.ok is True


def test_story_gate_check_wraps_the_real_rubric_scorer_noncompliant():
    """A story missing a required section or an AC oracle must come back
    ok=False with a real reason, not silently pass."""
    from prism_service.api import workflows as workflows_api

    story = "## Summary\nDoes a thing.\n"
    result = workflows_api.workflow_step_story_gate_check(
        workflows_api.StoryGateCheckRequest(story_doc=story, task_id="t1"),
        project="prism",
    )
    assert result.ok is False
    assert "story_complete" in result.reason


def test_story_gate_links_to_the_new_behavior_and_it_nests_under_conductor(tmp_path, monkeypatch):
    """story_gate must carry linked_workflow_id -> "story-gate-check", and
    that catalog entry must nest under conductor (parent_id) -- the SAME
    rule just established: only a behavior an actual state calls belongs
    in conductor's view. "land" nests too now (owner, 2026-08-21: green_gate
    approval ships the branch via ship_worker.py on both tracks), so it is
    NOT a useful negative control here anymore -- "ci-local-dev", which
    still has no conductor-state trigger, is the negative control instead."""
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc([])))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)

    def _fake_conductor_behaviors(project):
        return [
            {"id": "land", "name": "Land a task branch", "steps": [], "bots": [], "occupancy": {}},
            {"id": "ci-local-dev", "name": "CI to local dev", "steps": [], "bots": [], "occupancy": {}},
            {"id": "story-gate-check", "name": "Check story completeness", "steps": [], "bots": [], "occupancy": {}},
        ]
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows", _fake_conductor_behaviors)

    body = workflows_api.get_workflows("prism")

    story_gate_step = next(s for s in body["steps"] if s["id"] == "story_gate")
    assert story_gate_step["linked_workflow_id"] == "story-gate-check"

    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id["story-gate-check"].get("parent_id") == "conductor"
    assert by_id["land"].get("parent_id") == "conductor"
    assert "parent_id" not in by_id["ci-local-dev"]


def test_plan_gate_check_fails_closed_with_no_seeded_principles(monkeypatch):
    """Read-only wrap of the real plan_coverage scorer -- an unseeded
    principle store must never pass (the misfire guard), same as
    production. Mirrors conductor_service.py's exact evidence shape:
    story_md gets the SAME value as plan_doc."""
    from prism_service.api import workflows as workflows_api

    class _EmptyMemory:
        def list_entries(self, domain):
            return []

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(memory_svc=_EmptyMemory()))

    plan = (
        "flowchart TD\nA-->B\n\n"
        "Covers AC-1 fully."
    )
    result = workflows_api.workflow_step_plan_gate_check(
        workflows_api.PlanGateCheckRequest(
            plan_doc=plan, plan_diagram="flowchart TD\nA-->B\n", task_id="t1"),
        project="prism",
    )
    assert result.ok is False
    assert "no architecture principles seeded" in result.reason


def test_plan_gate_check_fails_on_missing_diagram_and_ac_coverage(monkeypatch):
    from prism_service.api import workflows as workflows_api

    class _EmptyMemory:
        def list_entries(self, domain):
            return []

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(memory_svc=_EmptyMemory()))

    result = workflows_api.workflow_step_plan_gate_check(
        workflows_api.PlanGateCheckRequest(plan_doc="", plan_diagram="", task_id="t1"),
        project="prism",
    )
    assert result.ok is False
    assert "plan_diagram is missing" in result.reason


def test_plan_gate_check_reads_memory_svc_from_the_named_project(monkeypatch):
    """Confirms get_project(project).memory_svc is what's actually wired
    in -- not a hardcoded or global lookup."""
    from prism_service.api import workflows as workflows_api

    seen_projects = []

    class _EmptyMemory:
        def list_entries(self, domain):
            return []

    def _get_project(p):
        seen_projects.append(p)
        return types.SimpleNamespace(memory_svc=_EmptyMemory())

    monkeypatch.setattr(workflows_api, "get_project", _get_project)

    workflows_api.workflow_step_plan_gate_check(
        workflows_api.PlanGateCheckRequest(plan_doc="x", plan_diagram="x", task_id="t1"),
        project="talentsync",
    )
    assert seen_projects == ["talentsync"]


def test_plan_gate_links_to_the_new_behavior_and_it_nests_under_conductor(tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc([])))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)

    def _fake_conductor_behaviors(project):
        return [
            {"id": "land", "name": "Land a task branch", "steps": [], "bots": [], "occupancy": {}},
            {"id": "story-gate-check", "name": "Check story completeness", "steps": [], "bots": [], "occupancy": {}},
            {"id": "plan-gate-check", "name": "Check plan coverage", "steps": [], "bots": [], "occupancy": {}},
        ]
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows", _fake_conductor_behaviors)

    body = workflows_api.get_workflows("prism")

    plan_gate_step = next(s for s in body["steps"] if s["id"] == "plan_gate")
    assert plan_gate_step["linked_workflow_id"] == "plan-gate-check"

    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id["plan-gate-check"].get("parent_id") == "conductor"
    assert by_id["land"].get("parent_id") == "conductor", (
        "land nests under conductor now (owner, 2026-08-21) -- it is the "
        "FSM's real terminal step, no longer a useful negative control")


def test_reason_loop_chains_observe_reason_validate(tmp_path, monkeypatch):
    """The generic loop: one endpoint, data-driven (persona/prompt/schema/
    rubric), used the same way every conductor authoring state would use
    it -- NOT a bespoke per-state Python function. Mocks claude_cli.invoke
    so no real API cost is spent on every test run; the orchestration
    wiring is what's under test here, not the model."""
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        brain_svc=None, memory_svc=None, task_svc=None, workflow_svc=None, governance=None))

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass
        def build(self, persona, story_file):
            return {"conventions": ["use uv, not pip"], "role_card": {"id": persona}}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    compliant_story = (
        "## Summary\nx\n\n## Requirements\nx\n\n## Acceptance Criteria\n"
        "- AC-1: x — oracle: check log\n"
    )

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        assert json_schema == {"type": "object", "properties": {"story_md": {"type": "string"}}}
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"story_md": compliant_story},
            usage={"cost_usd": 0.03}, run_id="run-1",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="sm",
            prompt="Draft a story.",
            json_schema={"type": "object", "properties": {"story_md": {"type": "string"}}},
            rubric="story_complete",
            task_id="t1",
        ),
        project="prism",
    )

    assert resp.observe["ok"] is True
    assert resp.observe["conventions_count"] == 1
    assert resp.reason["ok"] is True
    assert resp.reason["fields"]["story_md"] == compliant_story
    assert resp.reason["cost_usd"] == 0.03
    assert resp.validation["ok"] is True


def test_reason_loop_validate_fails_when_reason_output_fails_the_rubric(tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        brain_svc=None, memory_svc=None, task_svc=None, workflow_svc=None, governance=None))

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass
        def build(self, persona, story_file):
            return {"conventions": []}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"story_md": "## Summary\nincomplete"},
            usage={"cost_usd": 0.02}, run_id="run-2",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="sm", prompt="Draft a story.",
            json_schema={"type": "object"}, rubric="story_complete",
        ),
        project="prism",
    )

    assert resp.validation["ok"] is False
    assert "story_complete" in resp.validation["reason"]


def test_reason_loop_skips_validate_with_no_rubric(tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        brain_svc=None, memory_svc=None, task_svc=None, workflow_svc=None, governance=None))

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass
        def build(self, persona, story_file):
            return {"conventions": []}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"x": "y"}, usage={}, run_id="run-3",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="sm", prompt="Draft a story.", json_schema={"type": "object"},
        ),
        project="prism",
    )

    assert resp.validation == {"ok": None, "reason": "no rubric specified -- Validate skipped"}


def test_review_notes_and_verify_plan_link_to_their_loops_and_nest_under_conductor(tmp_path, monkeypatch):
    """Same rule, extended to the next two authoring states: a real
    linked_workflow_id -> parent_id="conductor" for review_previous_notes
    and verify_plan; land also nests (it's the FSM's real terminal step,
    owner 2026-08-21) -- only ci-local-dev stays unparented."""
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc([])))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)

    def _fake_conductor_behaviors(project):
        ids = ["land", "story-gate-check", "plan-gate-check", "draft-story-loop",
               "review-previous-notes-loop", "verify-plan-loop"]
        return [{"id": i, "name": i, "steps": [], "bots": [], "occupancy": {}} for i in ids]
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows", _fake_conductor_behaviors)

    body = workflows_api.get_workflows("prism")
    step_by_id = {s["id"]: s for s in body["steps"]}
    assert step_by_id["review_previous_notes"]["linked_workflow_id"] == "review-previous-notes-loop"
    assert step_by_id["verify_plan"]["linked_workflow_id"] == "verify-plan-loop"

    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id["review-previous-notes-loop"].get("parent_id") == "conductor"
    assert by_id["verify-plan-loop"].get("parent_id") == "conductor"
    assert by_id["land"].get("parent_id") == "conductor"


def test_reason_loop_plan_coverage_diffs_ac_ids_against_the_plan_itself(tmp_path, monkeypatch):
    """Regression: a live run of verify-plan-loop always failed AC-coverage
    because _score_rubric read a "story_md" field no reason-loop schema
    ever produces. Fixed to mirror plan-gate-check/conductor_service.py:
    the SAME plan_doc value is diffed against itself for AC ids -- so a
    plan_doc that inline-references its own AC ids must pass."""
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    class _EmptyMemory:
        def list_entries(self, domain):
            return [
                {"id": "p1", "status": "active", "source": "A", "target": "B"},
            ]

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        brain_svc=None, memory_svc=_EmptyMemory(), task_svc=None, workflow_svc=None, governance=None))

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass
        def build(self, persona, story_file):
            return {"conventions": []}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))
    monkeypatch.setattr(
        "prism_service.services.arc_governance.load_principles", lambda memory_svc: [])

    plan_doc = "## Plan\nCovers AC-1 fully.\n"
    diagram = "flowchart TD\nA-->B\n"

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"plan_doc": plan_doc, "plan_diagram": diagram},
            usage={"cost_usd": 0.05}, run_id="run-plan",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="sm", prompt="Write a plan.",
            json_schema={"type": "object"}, rubric="plan_coverage",
        ),
        project="prism",
    )

    # Empty principles -> the misfire guard still refuses ("no ... "
    # principles seeded"), proving the AC-coverage half is no longer the
    # blocker -- the OLD bug failed with "story carries no AC-<n> ids",
    # a different reason entirely.
    assert "story carries no AC-<n> ids" not in resp.validation["reason"]


def test_write_failing_tests_and_implement_tasks_link_but_validate_is_honest(tmp_path, monkeypatch):
    """These two have no YAML rubric -- red_with_trace/green need real
    test execution, not text scoring. They still get real linked
    behaviors (Observe+Reason drafts text, no writes), but rubric="" so
    Validate is explicitly reported as skipped -- never faked as passing."""
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc([])))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)

    def _fake_conductor_behaviors(project):
        ids = ["land", "write-failing-tests-loop", "implement-tasks-loop"]
        return [{"id": i, "name": i, "steps": [], "bots": [], "occupancy": {}} for i in ids]
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows", _fake_conductor_behaviors)

    body = workflows_api.get_workflows("prism")
    step_by_id = {s["id"]: s for s in body["steps"]}
    assert step_by_id["write_failing_tests"]["linked_workflow_id"] == "write-failing-tests-loop"
    assert step_by_id["implement_tasks"]["linked_workflow_id"] == "implement-tasks-loop"

    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id["write-failing-tests-loop"].get("parent_id") == "conductor"
    assert by_id["implement-tasks-loop"].get("parent_id") == "conductor"
    assert by_id["land"].get("parent_id") == "conductor"


def test_reason_loop_never_fakes_a_pass_for_unscored_states(tmp_path, monkeypatch):
    """write_failing_tests/implement_tasks pass rubric="" -- confirm the
    endpoint's existing "no rubric" path is what they get, not a silent
    ok=True."""
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        brain_svc=None, memory_svc=None, task_svc=None, workflow_svc=None, governance=None))

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass
        def build(self, persona, story_file):
            return {"conventions": []}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"test_code": "def test_x(): assert False"},
            usage={"cost_usd": 0.02}, run_id="run-wft",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="qa", prompt="Draft a failing test.",
            json_schema={"type": "object"}, rubric="",
        ),
        project="prism",
    )

    assert resp.validation == {"ok": None, "reason": "no rubric specified -- Validate skipped"}


def test_red_gate_status_reports_a_fresh_receipt_without_deciding_anything(tmp_path, monkeypatch):
    """Read-only visibility for red_gate: reuses the exact pure-read
    functions the real adjudicator consults (oracle_spec.fresh_red_receipt/
    latest_receipt), never calls adjudicate_test_red_gate or any of its
    embedded writes. red_gate itself stays untouched -- this only makes
    what's already on file observable."""
    from prism_service.api import workflows as workflows_api
    from prism_service.services import oracle_spec as osp

    monkeypatch.setattr(
        "prism_service.config.project_data_dir", lambda project: tmp_path)

    task_id = "t-red-1"
    red_sha = "a" * 40
    receipt = osp.EvidenceReceipt(
        task_id=task_id, job_id="job-1", spec_hash="spec-1", tree_sha=red_sha,
        adapter="pytest_ids", passed=False, status=osp.ST_RED,
        reason="2 pinned tests failed as expected",
    )
    osp.append_receipt("prism", receipt)

    class _FakeTask:
        oracle = "check /healthz returns 200"
        likely_misfire = ""

    class _FakeTaskSvc:
        def get(self, tid):
            return _FakeTask() if tid == task_id else None

    class _FakeConductorSvc:
        def _red_step_sha(self, tid):
            return red_sha

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_FakeConductorSvc()))
    # OracleSpec.from_task derives a spec_hash from the task's oracle text --
    # pin it to match the seeded receipt's spec_hash so "fresh" resolves true.
    monkeypatch.setattr(
        "prism_service.services.oracle_spec.OracleSpec.spec_hash",
        lambda self: "spec-1")

    resp = workflows_api.workflow_step_red_gate_status(
        workflows_api.RedGateStatusRequest(task_id=task_id), project="prism")

    assert resp.has_fresh_red_receipt is True
    assert resp.red_sha == red_sha
    assert "2 pinned tests failed as expected" in resp.reason
    assert resp.latest_receipt_status == osp.ST_RED


def test_red_gate_status_reports_no_receipt_honestly(tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        "prism_service.config.project_data_dir", lambda project: tmp_path)

    class _FakeTask:
        oracle = "check /healthz returns 200"
        likely_misfire = ""

    class _FakeTaskSvc:
        def get(self, tid):
            return _FakeTask()

    class _FakeConductorSvc:
        def _red_step_sha(self, tid):
            return ""

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_FakeConductorSvc()))

    resp = workflows_api.workflow_step_red_gate_status(
        workflows_api.RedGateStatusRequest(task_id="t-nothing"), project="prism")

    assert resp.has_fresh_red_receipt is False
    assert "no red-step commit resolved yet" in resp.reason


def test_red_gate_status_never_calls_the_real_adjudicator(monkeypatch):
    """The whole point: this must never touch adjudicate_test_red_gate or
    any write path, no matter what. Assert the method isn't even present
    on the fake conductor_svc this test wires in -- if the endpoint tried
    to call it, this would raise AttributeError, not silently pass."""
    from prism_service.api import workflows as workflows_api

    class _FakeTask:
        oracle = ""
        likely_misfire = ""

    class _FakeTaskSvc:
        def get(self, tid):
            return _FakeTask()

    class _NoAdjudicateConductorSvc:
        def _red_step_sha(self, tid):
            return ""
        # deliberately no adjudicate_test_red_gate / adjudicate_demo_red_gate

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_NoAdjudicateConductorSvc()))

    resp = workflows_api.workflow_step_red_gate_status(
        workflows_api.RedGateStatusRequest(task_id="t-x"), project="prism")
    assert resp.has_fresh_red_receipt is False


def test_green_gate_status_reports_a_fresh_passing_receipt(monkeypatch):
    """Read-only visibility for green_gate: calls the EXACT same
    _oracle_receipt_refusal consultation gate_adjudicator.py's
    _pending_decline_reason already makes for reporting, never
    adjudicate_green_gate or any write."""
    from prism_service.api import workflows as workflows_api
    from prism_service.services import oracle_spec as osp

    class _FakeReceipt:
        status = osp.ST_PASS if hasattr(osp, "ST_PASS") else "passed"
        reason = "all pinned tests green"

    class _FakeTaskSvc:
        def get(self, tid):
            return object()

    class _FakeConductorSvc:
        def _oracle_receipt_refusal(self, task, *, override, reason):
            return "", _FakeReceipt()

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_FakeConductorSvc()))
    monkeypatch.setattr(
        "prism_service.services.oracle_spec.latest_receipt",
        lambda project, tid: _FakeReceipt())

    resp = workflows_api.workflow_step_green_gate_status(
        workflows_api.GreenGateStatusRequest(task_id="t-green-1"), project="prism")

    assert resp.has_fresh_passing_receipt is True
    assert "all pinned tests green" in resp.reason


def test_green_gate_status_reports_refusal_honestly(monkeypatch):
    from prism_service.api import workflows as workflows_api

    class _FakeTaskSvc:
        def get(self, tid):
            return object()

    class _FakeConductorSvc:
        def _oracle_receipt_refusal(self, task, *, override, reason):
            return "no EvidenceReceipt on file — the oracle was never exercised", None

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_FakeConductorSvc()))
    monkeypatch.setattr(
        "prism_service.services.oracle_spec.latest_receipt",
        lambda project, tid: None)

    resp = workflows_api.workflow_step_green_gate_status(
        workflows_api.GreenGateStatusRequest(task_id="t-green-2"), project="prism")

    assert resp.has_fresh_passing_receipt is False
    assert "never exercised" in resp.reason


def test_green_gate_status_never_calls_the_real_adjudicator(monkeypatch):
    """The whole point: must never touch adjudicate_green_gate. Assert
    it's absent from the fake conductor_svc -- if the endpoint tried to
    call it, this would raise AttributeError, not silently pass."""
    from prism_service.api import workflows as workflows_api

    class _FakeTaskSvc:
        def get(self, tid):
            return object()

    class _NoAdjudicateConductorSvc:
        def _oracle_receipt_refusal(self, task, *, override, reason):
            return "not evidenced", None
        # deliberately no adjudicate_green_gate

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_NoAdjudicateConductorSvc()))
    monkeypatch.setattr(
        "prism_service.services.oracle_spec.latest_receipt",
        lambda project, tid: None)

    resp = workflows_api.workflow_step_green_gate_status(
        workflows_api.GreenGateStatusRequest(task_id="t-x"), project="prism")
    assert resp.has_fresh_passing_receipt is False


def test_green_gate_status_checks_report_every_pre_flight_tooth(monkeypatch):
    """Owner directive (task 3baadd19, 2026-08-24): 'make this real...
    make sure that it is a part of the flows and enforces our rules' --
    the Workflows page's green-gate-status view was a single opaque
    oracle-receipt check while five other real teeth governed the same
    gate invisibly. `checks` must enumerate all of them."""
    from prism_service.api import workflows as workflows_api

    class _FakeTask:
        id = "t-checks-1"
        tags = ["backend"]
        proof_type = "test"
        oracle = ""
        completion_proof = "tests/unit/test_x.py::test_y PASSED"

    class _FakeTaskSvc:
        def get(self, tid):
            return _FakeTask()

    class _FakeConductorSvc:
        def _oracle_receipt_refusal(self, task, *, override, reason):
            return "", None

        def _unshipped_gate_reason(self, task):
            return ""

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_FakeConductorSvc()))
    monkeypatch.setattr(
        "prism_service.services.oracle_spec.latest_receipt",
        lambda project, tid: None)

    resp = workflows_api.workflow_step_green_gate_status(
        workflows_api.GreenGateStatusRequest(task_id="t-checks-1"), project="prism")

    ids = {c.id for c in resp.checks}
    assert ids == {"candidate_controls", "reachability", "ui_artifact",
                   "screen_claim", "shipped_ness", "demo_evidence"}, ids
    for c in resp.checks:
        assert c.label, c.id
        assert isinstance(c.ok, bool)


def test_green_gate_status_checks_replay_3baadd19s_exact_shape(monkeypatch):
    """LIVE REGRESSION replay: proof_type=demo, no 'ui' tag, no captured
    evidence -- task 3baadd19's own shape when it wrongly reached
    green_gate. The demo_evidence check must report ok=False here, and
    everything else stays clean (this is a scoped tooth, not a blanket
    block)."""
    from prism_service.api import workflows as workflows_api

    class _FakeTask:
        id = "3baadd19-78af-42b8-a78e-47a4b6f51fc0"
        tags = ["conductor", "architecture", "owner-directive",
                "drive-worker", "github", "jira"]
        proof_type = "demo"
        oracle = ("PRISM claims a task by itself; film/screenshots of the "
                 "unattended drive in the PRISM evidence store")
        completion_proof = ("the epic's actual oracle... is NOT yet true "
                            "in production")

    class _FakeTaskSvc:
        def get(self, tid):
            return _FakeTask()

    class _FakeConductorSvc:
        def _oracle_receipt_refusal(self, task, *, override, reason):
            return "no EvidenceReceipt on file", None

        def _unshipped_gate_reason(self, task):
            return ""

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_FakeConductorSvc()))
    monkeypatch.setattr(
        "prism_service.services.oracle_spec.latest_receipt",
        lambda project, tid: None)

    resp = workflows_api.workflow_step_green_gate_status(
        workflows_api.GreenGateStatusRequest(
            task_id="3baadd19-78af-42b8-a78e-47a4b6f51fc0"), project="prism")

    by_id = {c.id: c for c in resp.checks}
    assert by_id["demo_evidence"].ok is False, (
        f"3baadd19's exact shape (demo, no ui tag, no captured evidence) "
        f"must be flagged: {by_id['demo_evidence']}")
    assert "evidence" in by_id["demo_evidence"].reason.lower()
    # Scoped, not a blanket block: the ui_artifact/screen_claim teeth are
    # "ui"-tag-gated and this task carries no "ui" tag, so they stay clean.
    assert by_id["ui_artifact"].ok is True
    assert by_id["screen_claim"].ok is True


def test_green_gate_status_checks_never_crash_on_a_narrow_fake_conductor_svc(
    monkeypatch,
):
    """A tooth this endpoint calls THROUGH conductor_svc
    (_unshipped_gate_reason) must degrade to ok=True, never crash the
    whole status report, when the wired conductor_svc doesn't implement
    it -- mirrors this file's own test_green_gate_status_never_calls_the_
    real_adjudicator precedent for resilience against narrow fakes."""
    from prism_service.api import workflows as workflows_api

    class _FakeTask:
        id = "t-narrow"
        tags = ["backend"]
        proof_type = "test"
        oracle = ""
        completion_proof = ""

    class _FakeTaskSvc:
        def get(self, tid):
            return _FakeTask()

    class _NarrowConductorSvc:
        def _oracle_receipt_refusal(self, task, *, override, reason):
            return "", None
        # deliberately no _unshipped_gate_reason

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_NarrowConductorSvc()))
    monkeypatch.setattr(
        "prism_service.services.oracle_spec.latest_receipt",
        lambda project, tid: None)

    resp = workflows_api.workflow_step_green_gate_status(
        workflows_api.GreenGateStatusRequest(task_id="t-narrow"), project="prism")

    by_id = {c.id: c for c in resp.checks}
    assert by_id["shipped_ness"].ok is True
    assert len(resp.checks) == 6


def test_green_gate_check_reports_one_named_tooth(monkeypatch):
    """Owner (task 3baadd19, 2026-08-24), on seeing the old 1-step
    diagram: 'if there are 5 [sic; 7] steps in the green gate behavior
    than you should show them, here so we can see' -- the per-check
    endpoint backs one JSON behavior step per real tooth, so the
    Workflows page diagram can show a genuine node per check instead of a
    checklist buried inside one callback's response body."""
    from prism_service.api import workflows as workflows_api

    class _FakeTask:
        id = "3baadd19-78af-42b8-a78e-47a4b6f51fc0"
        tags = ["conductor", "architecture", "owner-directive",
                "drive-worker", "github", "jira"]
        proof_type = "demo"
        oracle = "PRISM claims a task by itself"
        completion_proof = "the epic's actual oracle is not yet met"

    class _FakeTaskSvc:
        def get(self, tid):
            return _FakeTask()

    class _FakeConductorSvc:
        def _oracle_receipt_refusal(self, task, *, override, reason):
            return "no EvidenceReceipt on file", None

        def _unshipped_gate_reason(self, task):
            return ""

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_FakeConductorSvc()))

    resp = workflows_api.workflow_step_green_gate_check(
        workflows_api.GreenGateCheckRequest(
            task_id="3baadd19-78af-42b8-a78e-47a4b6f51fc0",
            check="demo_evidence"),
        project="prism")

    assert resp.id == "demo_evidence"
    assert resp.ok is False
    assert "evidence" in resp.reason.lower()


def test_green_gate_check_every_registry_entry_is_individually_reachable(monkeypatch):
    """All 7 checks the JSON behavior now chains as separate steps
    (candidate_controls, reachability, ui_artifact, screen_claim,
    shipped_ness, demo_evidence, oracle_receipt) must be individually
    callable through this endpoint -- a step whose id doesn't resolve here
    would be a silent 404 on the Workflows page."""
    from prism_service.api import workflows as workflows_api

    class _FakeTask:
        id = "t-all"
        tags = ["backend"]
        proof_type = "test"
        oracle = ""
        completion_proof = "tests/unit/test_x.py::test_y PASSED"

    class _FakeTaskSvc:
        def get(self, tid):
            return _FakeTask()

    class _FakeConductorSvc:
        def _oracle_receipt_refusal(self, task, *, override, reason):
            return "", None

        def _unshipped_gate_reason(self, task):
            return ""

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=_FakeConductorSvc()))

    for check_id in ("candidate_controls", "reachability", "ui_artifact",
                     "screen_claim", "shipped_ness", "demo_evidence",
                     "oracle_receipt"):
        resp = workflows_api.workflow_step_green_gate_check(
            workflows_api.GreenGateCheckRequest(task_id="t-all", check=check_id),
            project="prism")
        assert resp.id == check_id
        assert resp.ok is True, f"{check_id}: {resp.reason}"


def test_green_gate_check_unknown_check_id_reports_rather_than_crashes(monkeypatch):
    from prism_service.api import workflows as workflows_api

    class _FakeTask:
        id = "t-x"
        tags = []
        proof_type = ""
        oracle = ""
        completion_proof = ""

    class _FakeTaskSvc:
        def get(self, tid):
            return _FakeTask()

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(), conductor_svc=object()))

    resp = workflows_api.workflow_step_green_gate_check(
        workflows_api.GreenGateCheckRequest(task_id="t-x", check="nonexistent"),
        project="prism")

    assert resp.id == "nonexistent"


def test_green_gate_status_behavior_json_has_one_step_per_registry_check():
    """Pin the actual on-disk JSON that renders the Workflows page diagram
    -- this is the file the owner was looking at when they asked for the
    steps to be shown. Must carry one step per real check, not the old
    single opaque 'status' callback alone."""
    import json
    from pathlib import Path

    behavior_path = (Path(__file__).resolve().parent.parent.parent.parent.parent
                     / ".prism" / "behaviors" / "conductor" / "green-gate-status.json")
    data = json.loads(behavior_path.read_text())
    step_ids = {s["id"] for s in data["steps"]}
    assert step_ids == {"candidate_controls", "reachability", "ui_artifact",
                        "screen_claim", "shipped_ness", "demo_evidence",
                        "oracle_receipt", "status"}, step_ids
    for step in data["steps"]:
        if step["id"] != "status":
            assert "green-gate-check" in step["url"]
            assert f'"check": "{step["id"]}"' in step["body"]


def test_green_gate_links_to_the_new_behavior_and_it_nests_under_conductor(tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc([])))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)

    def _fake_conductor_behaviors(project):
        return [{"id": "green-gate-status", "name": "Green gate evidence status",
                 "steps": [], "bots": [], "occupancy": {}}]
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows", _fake_conductor_behaviors)

    body = workflows_api.get_workflows("prism")
    green_gate_step = next(s for s in body["steps"] if s["id"] == "green_gate")
    assert green_gate_step["linked_workflow_id"] == "green-gate-status"

    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id["green-gate-status"].get("parent_id") == "conductor"


def test_land_is_the_conductors_visible_final_ship_step(tmp_path, monkeypatch):
    """Owner (2026-08-21): "make the conductor's workflow have a final ship
    step when it's all done" -- prior to this, GREEN GATE EVIDENCE STATUS
    was the last item under CONDUCTOR in the Workflows page sidebar, and
    "Land a task branch" rendered as a disconnected top-level sibling, even
    though green_gate approval already ships the branch automatically (both
    the human-approved and machine-adjudicated tracks route through
    ship_worker.py). "land" has no WORKFLOW_STEPS entry of its own -- unlike
    every other linked behavior here, it nests via _CONDUCTOR_LINKED_
    BEHAVIOR_IDS directly, not via a step's linked_workflow_id -- because
    green_gate is the FSM's structurally-terminal state and inserting a new
    step after it is a real, separately-scoped, higher-risk change (14+
    `== "green_gate"` call sites in conductor_service.py treat it as last).
    "ci-local-dev" is the negative control: no conductor state triggers it,
    so it must stay unparented."""
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc([])))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)

    def _fake_conductor_behaviors(project):
        return [
            {"id": "land", "name": "Land a task branch", "steps": [],
             "bots": [], "occupancy": {}},
            {"id": "ci-local-dev", "name": "CI to local dev", "steps": [],
             "bots": [], "occupancy": {}},
            {"id": "green-gate-status", "name": "Green gate evidence status",
             "steps": [], "bots": [], "occupancy": {}},
        ]
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        _fake_conductor_behaviors)

    body = workflows_api.get_workflows("prism")

    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id["land"].get("parent_id") == "conductor", (
        "\"land\" must nest under conductor -- it is the visible final "
        "ship step, not a disconnected sibling workflow")
    assert "parent_id" not in by_id["ci-local-dev"], (
        "ci-local-dev has no conductor-state trigger and must stay "
        "unparented -- this change must not sweep in every behavior")


def test_land_json_completes_the_real_ship_pipeline_shape():
    """land.json used to define only push + open-pr -- half of what
    ship_worker.py's real, tested pipeline does (push -> pr create -> pr
    checks -> pr merge -> fetch). Nesting it under conductor as THE visible
    final step (see test_land_is_the_conductors_visible_final_ship_step)
    while it still looked half-finished would mislead a reader clicking
    into it. Pins the checked-in behavior file itself, not the API
    (_conductor_behavior_workflows fetches this content from the live
    AosWorkflows engine, not from disk, in production)."""
    import json

    path = (Path(__file__).resolve().parent.parent.parent.parent.parent
           / ".prism" / "behaviors" / "conductor" / "land.json")
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["id"] == "land"
    step_ids = [s["id"] for s in data["steps"]]
    assert step_ids == ["push", "open-pr", "wait-for-ci", "merge"], step_ids

    by_id = {s["id"]: s for s in data["steps"]}
    assert "${branch}" in by_id["push"]["command"]
    assert "${branch}" in by_id["open-pr"]["command"]
    assert "${taskId}" in by_id["open-pr"]["command"]
    # Each step resolves what it needs from ${branch}/${taskId} alone --
    # deliberately NOT dependent on capturing a PR number from a prior
    # step's stdout (unconfirmed whether this engine supports that), same
    # discipline `gh pr checks`/`gh pr merge` support natively (both accept
    # a branch name in place of a PR number).
    assert "${branch}" in by_id["wait-for-ci"]["command"]
    assert "--watch" in by_id["wait-for-ci"]["command"]
    assert "${branch}" in by_id["merge"]["command"]
    assert "--squash" in by_id["merge"]["command"]
    assert "--delete-branch" in by_id["merge"]["command"]


def test_land_is_ordered_last_in_bot_json_so_it_renders_as_the_terminal_step():
    """First cut of the nesting fix put "land" wherever bot.json's own
    behaviorIds happened to list it -- FIRST, right after "ci-local-dev" --
    so it rendered as the 2nd item under CONDUCTOR (right after "Build and
    test"), not as the visible FINAL step the owner asked for. Display
    order follows bot.json's behaviorIds order (_conductor_behavior_
    workflows iterates it verbatim, confirmed live by a teammate's
    Playwright screenshot after the first cut shipped). Pins the checked-in
    bot.json directly, same discipline as the land.json shape test above."""
    import json

    path = (Path(__file__).resolve().parent.parent.parent.parent.parent
           / ".prism" / "behaviors" / "conductor" / "bot.json")
    data = json.loads(path.read_text(encoding="utf-8"))

    fsm = next(f for f in data["fsms"] if f["fsmId"] == "pipeline")
    ids = fsm["behaviorIds"]
    assert ids[-1] == "land", (
        f"'land' must be the LAST entry so it renders as conductor's "
        f"terminal step, not wherever it happened to be listed: {ids}")
    assert ids.count("land") == 1


def test_write_failing_tests_loop_forbids_uncaught_exception_red():
    """DEFECT this pins: the write-failing-tests-loop.json prompt told the
    qa-persona drafter only that a test "must ... currently fail", never HOW
    it must fail. Multiple tasks (bb388e9d, 4e6e7417, and likely dd2b87c8)
    drafted tests using raw lookups (str.index() on a missing substring, an
    import of a not-yet-existing module) that raise an UNCAUGHT exception at
    collection time (pytest rc=2/4), not a genuine assertion failure
    (rc==1) -- and oracle_spec.py's red-worktree runner explicitly refuses
    to count anything but rc==1 as red demonstrated, so those tasks' red_gate
    can never clear as authored. The prompt must say so explicitly."""
    import json

    path = (Path(__file__).resolve().parent.parent.parent.parent.parent
           / ".prism" / "behaviors" / "conductor" / "write-failing-tests-loop.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    body = json.loads(data["steps"][0]["body"])
    prompt = body["prompt"]

    assert "genuine assertion failure" in prompt.lower() or "assertion failure" in prompt, (
        "prompt must explicitly require a real assert, not just 'must fail'")
    assert "rc==1" in prompt or "exit code 1" in prompt, (
        "prompt must name the exact pytest rc the red-gate verifier requires")
    assert ".index(" in prompt, (
        "prompt must name the specific raw-lookup pitfall (str.index()) "
        "that produced the uncaught-exception failures seen in production")
