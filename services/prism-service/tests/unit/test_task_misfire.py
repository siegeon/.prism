"""likely_misfire field — records & audits how a task could pass-but-be-wrong.

Sibling of test_task_oracle.py. Pins, RED-first (task 7bdb5701):
  * likely_misfire defaults to "" and round-trips through the SAME DB
    serialize/deserialize path as the oracle block (additive/non-breaking).
  * The MCP dispatcher SEAM (handle_tool -> task_create / task_update)
    plumbs likely_misfire end-to-end — not just the service method.
  * The API SEAM (POST create / GET / PATCH update) accepts & echoes
    likely_misfire — a real route field, not headless plumbing.
  * green_gate emits an ADVISORY note (annotate, never block) when a
    misfire is recorded but completion_proof does not visibly address it,
    and is SILENT when the misfire is addressed — mirroring
    green_gate_proof_note.

These FAIL before the field/note/wiring exist (AttributeError /
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
    SAME tmp-backed task_svc (mirrors test_mcp_graph_annotate)."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-misfire"
    pc._contexts.clear()


# ---- field round-trip (unit contract) ---------------------------------

def test_likely_misfire_defaults_empty(tmp_path):
    task_svc, _ = _services(tmp_path)
    t = task_svc.create(title="x")
    assert t.likely_misfire == ""
    g = task_svc.get(t.id)
    assert g.likely_misfire == ""


def test_likely_misfire_round_trip_create_and_update(tmp_path):
    task_svc, _ = _services(tmp_path)
    t = task_svc.create(
        title="x", likely_misfire="tests stub the API; dead UI passes green"
    )
    assert t.likely_misfire == "tests stub the API; dead UI passes green"
    g = task_svc.get(t.id)
    assert g.likely_misfire == "tests stub the API; dead UI passes green"
    task_svc.update(t.id, likely_misfire="updated misfire risk")
    g2 = task_svc.get(t.id)
    assert g2.likely_misfire == "updated misfire risk"


# ---- MCP dispatcher SEAM (handle_tool, not the service method) ---------

def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def test_task_create_schema_advertises_likely_misfire():
    from prism_service.mcp.tools import TOOLS
    by_name = {t.name: t for t in TOOLS}
    assert "likely_misfire" in by_name["task_create"].inputSchema["properties"]
    assert "likely_misfire" in by_name["task_update"].inputSchema["properties"]


def test_mcp_task_create_and_update_plumbs_likely_misfire(project):
    from prism_service.project_context import get_project

    created = json.loads(_call(
        "task_create",
        {"title": "misfire via mcp", "likely_misfire": "passes on stubbed seam"},
        project,
    ))
    tid = created["id"]
    assert created["likely_misfire"] == "passes on stubbed seam"

    # Survives a SEPARATE read through the project context (durable, not
    # an in-memory echo).
    stored = get_project(project).task_svc.get(tid)
    assert stored.likely_misfire == "passes on stubbed seam"

    json.loads(_call(
        "task_update",
        {"id": tid, "likely_misfire": "revised misfire"},
        project,
    ))
    assert get_project(project).task_svc.get(tid).likely_misfire == "revised misfire"


# ---- API SEAM (real route, POST/GET/PATCH) ----------------------------

def _api_client(project_id):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app), project_id


def test_api_create_get_patch_round_trips_likely_misfire(project):
    client, pid = _api_client(project)

    post = client.post(
        "/api/tasks", params={"project": pid},
        json={"title": "api misfire", "likely_misfire": "API field is dead"},
    )
    assert post.status_code in (200, 201), post.text
    tid = post.json()["task"]["id"]

    got = client.get(f"/api/tasks/{tid}", params={"project": pid})
    assert got.status_code == 200, got.text
    assert got.json()["task"]["likely_misfire"] == "API field is dead"

    patched = client.patch(
        f"/api/tasks/{tid}", params={"project": pid},
        json={"likely_misfire": "patched misfire"},
    )
    assert patched.status_code == 200, patched.text
    again = client.get(f"/api/tasks/{tid}", params={"project": pid})
    assert again.json()["task"]["likely_misfire"] == "patched misfire"


# ---- green_gate advisory misfire note ---------------------------------

def test_green_gate_misfire_note_fires_when_unaddressed():
    from prism_service.services.conductor_service import green_gate_misfire_note
    note = green_gate_misfire_note(
        likely_misfire="UI is dead; stubbed API test still passes",
        completion_proof="ran pytest; 12 passed; screenshot :8888/x.png",
    )
    assert note  # non-empty advisory
    assert "misfire" in note.lower()


def test_green_gate_misfire_note_silent_when_addressed():
    from prism_service.services.conductor_service import green_gate_misfire_note
    # Proof visibly references the misfire risk -> no advisory.
    note = green_gate_misfire_note(
        likely_misfire="dead UI passes",
        completion_proof="verified the UI is not dead: screenshot shows the "
                         "rendered card addresses the dead-UI passes risk",
    )
    assert note == ""


def test_green_gate_misfire_note_silent_when_no_misfire():
    from prism_service.services.conductor_service import green_gate_misfire_note
    assert green_gate_misfire_note("", "anything") == ""


# ---- integration: note rides the green_gate decision ------------------

def _drive_to_green_gate(task_svc, cond, t):
    guard = 0
    while task_svc.get(t.id).workflow_step != "green_gate" and guard < 20:
        if task_svc.get(t.id).gate_state == "pending":
            cond.gate_decide(
                t.id, "approve",
                reason="walk; independent re-run: pytest -> 1 failed",
                override=True, actor="walk-bot", session_id="walk-bot")
        else:
            cond.advance_task(t.id, validation="step")
        guard += 1
    assert task_svc.get(t.id).workflow_step == "green_gate"


def test_green_gate_annotates_unaddressed_misfire(tmp_path):
    task_svc, cond = _services(tmp_path)
    # Real artifact proof (clears the proof-carrying tooth) that does NOT
    # mention the recorded misfire risk -> advisory should fire.
    t = task_svc.create(
        title="misfire integ",
        likely_misfire="the stubbed API test passes even with a dead route",
        completion_proof="full suite green; screenshot at :8888/card.png",
    )
    _drive_to_green_gate(task_svc, cond, t)
    res = cond.gate_decide(t.id, "approve", reason="full green", override=True)
    assert res["ok"] is True, res
    assert "misfire" in task_svc.get(t.id).gate_reason.lower()
