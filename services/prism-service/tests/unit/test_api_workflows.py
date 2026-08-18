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


def _client(svc, monkeypatch, data_dir):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import workflows as workflows_api

    monkeypatch.setenv("PRISM_DATA_DIR", str(data_dir))
    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=svc))
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
                      "persona_label"):
            assert field in s, f"step {s.get('id')!r} is missing {field!r}: {s}"
        assert s["type"] in ("agent", "gate"), (
            f"step {s['id']} has an unknown type {s['type']!r}")


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
    app = FastAPI()
    app.include_router(workflows_api.router, prefix="/api/workflows")
    client = TestClient(app)

    assert client.get(f"/api/workflows?project={project}").status_code == 200
    assert seen == [project], (
        f"the endpoint must resolve the REQUESTED project, saw {seen}")
