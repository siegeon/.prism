"""full_outcome_complete — distinguishes a green SLICE from a finished OUTCOME.

GoalBuddy GAP-4 (task 3619859f). Sibling of test_task_misfire.py. Pins,
RED-first:
  * full_outcome_complete defaults to False and round-trips through the SAME
    additive DB path as the oracle/likely_misfire block (stored 0/1 bool).
  * The MCP dispatcher SEAM (handle_tool -> task_create / task_update) plumbs
    full_outcome_complete end-to-end — schema + arg plumbing, not just the
    service method (a dead boolean defined-but-undispatched is the failure).
  * The API SEAM (POST create / GET / PATCH update) accepts & echoes the
    field — a real route field, not headless plumbing.
  * conductor_service exposes pure, unit-testable module-level helpers
    full_outcome_verdict() / green_gate_outcome_note() mirroring
    green_gate_proof_note / green_gate_misfire_note, reusing is_weak_proof.
  * Verdict semantics: owner-outcome-complete is True ONLY when the slice is
    green AND there are no incomplete (non-cancelled) child tasks AND the
    completion_proof is strong; otherwise False with a concrete reason.
  * The advisory note is CONDITIONAL — appended at green_gate ONLY when the
    slice is green but the owner outcome is demonstrably incomplete (weak
    proof or incomplete children); SILENT when the outcome is satisfied.
    Annotate, never block (hooks doctrine).

These FAIL before the field/helpers/wiring exist (AttributeError /
ImportError / missing field in JSON), which is the point of the red step.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc
    )
    return task_svc, cond


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext so handle_tool + the API router resolve the
    SAME tmp-backed task_svc (mirrors test_task_misfire)."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    # get_project no longer creates on miss (d37193da):
    # seed the sandboxed project dir explicitly.
    cfg.project_data_dir("test-outcome")
    pc._contexts.clear()
    yield "test-outcome"
    pc._contexts.clear()


# ---- field round-trip (unit contract) ---------------------------------

def test_full_outcome_complete_defaults_false(tmp_path):
    task_svc, _ = _services(tmp_path)
    t = task_svc.create(title="x")
    assert t.full_outcome_complete is False
    g = task_svc.get(t.id)
    assert g.full_outcome_complete is False


def test_full_outcome_complete_round_trip_create_and_update(tmp_path):
    task_svc, _ = _services(tmp_path)
    t = task_svc.create(title="x", full_outcome_complete=True)
    assert t.full_outcome_complete is True
    g = task_svc.get(t.id)
    # Stored as 0/1 but reads back as a real bool, unchanged.
    assert g.full_outcome_complete is True
    task_svc.update(t.id, full_outcome_complete=False)
    g2 = task_svc.get(t.id)
    assert g2.full_outcome_complete is False


# ---- MCP dispatcher SEAM (handle_tool, not the service method) ---------

def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def test_task_create_update_schema_advertise_full_outcome_complete():
    from prism_service.mcp.tools import TOOLS
    by_name = {t.name: t for t in TOOLS}
    assert "full_outcome_complete" in by_name["task_create"].inputSchema["properties"]
    assert "full_outcome_complete" in by_name["task_update"].inputSchema["properties"]


def test_mcp_task_create_and_update_plumbs_full_outcome_complete(project):
    from prism_service.project_context import get_project

    created = json.loads(_call(
        "task_create",
        {"title": "outcome via mcp", "full_outcome_complete": True},
        project,
    ))
    tid = created["id"]
    assert created["full_outcome_complete"] is True

    # Survives a SEPARATE read through the project context (durable, not echo).
    stored = get_project(project).task_svc.get(tid)
    assert stored.full_outcome_complete is True

    json.loads(_call(
        "task_update",
        {"id": tid, "full_outcome_complete": False},
        project,
    ))
    assert get_project(project).task_svc.get(tid).full_outcome_complete is False


# ---- API SEAM (real route, POST/GET/PATCH) ----------------------------

def _api_client(project_id):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app), project_id


def test_api_create_get_patch_round_trips_full_outcome_complete(project):
    client, pid = _api_client(project)

    post = client.post(
        "/api/tasks", params={"project": pid},
        json={"title": "api outcome", "full_outcome_complete": True},
    )
    assert post.status_code in (200, 201), post.text
    tid = post.json()["task"]["id"]
    assert post.json()["task"]["full_outcome_complete"] is True

    got = client.get(f"/api/tasks/{tid}", params={"project": pid})
    assert got.status_code == 200, got.text
    assert got.json()["task"]["full_outcome_complete"] is True

    patched = client.patch(
        f"/api/tasks/{tid}", params={"project": pid},
        json={"full_outcome_complete": False},
    )
    assert patched.status_code == 200, patched.text
    again = client.get(f"/api/tasks/{tid}", params={"project": pid})
    assert again.json()["task"]["full_outcome_complete"] is False


# ---- verdict helpers (pure, unit-testable in isolation) ---------------

def test_full_outcome_verdict_complete_when_green_strong_no_children():
    from prism_service.services.conductor_service import full_outcome_verdict
    complete, reason = full_outcome_verdict(
        slice_green=True,
        completion_proof="full suite green; screenshot at :8888/card.png",
        incomplete_children=0,
    )
    assert complete is True
    assert reason == ""


def test_full_outcome_verdict_incomplete_on_weak_proof():
    from prism_service.services.conductor_service import full_outcome_verdict
    complete, reason = full_outcome_verdict(
        slice_green=True, completion_proof="", incomplete_children=0,
    )
    assert complete is False
    assert reason  # concrete reason mentioning the weak proof


def test_full_outcome_verdict_incomplete_on_open_children():
    from prism_service.services.conductor_service import full_outcome_verdict
    complete, reason = full_outcome_verdict(
        slice_green=True,
        completion_proof="full suite green; screenshot at :8888/x.png",
        incomplete_children=2,
    )
    assert complete is False
    assert "2" in reason  # names how many incomplete child tasks


def test_full_outcome_verdict_incomplete_when_slice_not_green():
    from prism_service.services.conductor_service import full_outcome_verdict
    complete, _ = full_outcome_verdict(
        slice_green=False,
        completion_proof="full suite green; screenshot at :8888/x.png",
        incomplete_children=0,
    )
    assert complete is False


# ---- advisory note (conditional — silent when outcome satisfied) ------

def test_green_gate_outcome_note_silent_when_outcome_complete():
    from prism_service.services.conductor_service import green_gate_outcome_note
    note = green_gate_outcome_note(
        slice_green=True,
        completion_proof="full suite green; screenshot at :8888/card.png",
        incomplete_children=0,
    )
    assert note == ""


def test_green_gate_outcome_note_fires_on_weak_proof():
    from prism_service.services.conductor_service import green_gate_outcome_note
    note = green_gate_outcome_note(
        slice_green=True, completion_proof="", incomplete_children=0,
    )
    assert note
    assert "owner-outcome" in note.lower()


def test_green_gate_outcome_note_fires_on_incomplete_children():
    from prism_service.services.conductor_service import green_gate_outcome_note
    note = green_gate_outcome_note(
        slice_green=True,
        completion_proof="full suite green; screenshot at :8888/x.png",
        incomplete_children=3,
    )
    assert note
    assert "owner-outcome" in note.lower()


# ---- integration: note rides the green_gate decision, conditionally ----

def _drive_to_green_gate(task_svc, cond, t):
    guard = 0
    cleared = 0
    while task_svc.get(t.id).workflow_step != "green_gate" and guard < 30:
        if task_svc.get(t.id).gate_state == "pending":
            # DISTINCT actor per clear (task 8579d49e): story_gate/plan_gate
            # precede green_gate; a reused actor self-overrides -> refused.
            cleared += 1
            cond.gate_decide(
                t.id, "approve",
                reason="walk; independent re-run: pytest -> 1 failed",
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}")
        else:
            cond.advance_task(t.id, validation="step")
        guard += 1
    assert task_svc.get(t.id).workflow_step == "green_gate"


def test_green_gate_annotates_slice_only_when_child_incomplete(tmp_path):
    task_svc, cond = _services(tmp_path)
    # Strong artifact proof (clears the proof-carrying tooth) — but an OPEN
    # child task means the owner outcome is NOT complete -> advisory fires.
    t = task_svc.create(
        title="parent slice",
        completion_proof="full suite green; screenshot at :8888/card.png",
    )
    task_svc.create(title="open child", parent_id=t.id)  # status pending
    _drive_to_green_gate(task_svc, cond, t)
    res = cond.gate_decide(t.id, "approve", reason="full green", override=True)
    assert res["ok"] is True, res
    reason = task_svc.get(t.id).gate_reason.lower()
    assert "owner-outcome" in reason
    # The field is set truthfully: NOT complete with an open child.
    assert task_svc.get(t.id).full_outcome_complete is False


def test_green_gate_silent_and_marks_complete_when_outcome_met(tmp_path):
    task_svc, cond = _services(tmp_path)
    # Strong proof + no children -> owner outcome complete -> NO advisory,
    # and full_outcome_complete is set True.
    t = task_svc.create(
        title="finished outcome",
        completion_proof="full suite green; screenshot at :8888/done.png",
    )
    _drive_to_green_gate(task_svc, cond, t)
    res = cond.gate_decide(t.id, "approve", reason="full green", override=True)
    assert res["ok"] is True, res
    assert "owner-outcome" not in task_svc.get(t.id).gate_reason.lower()
    assert task_svc.get(t.id).full_outcome_complete is True
