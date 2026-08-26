"""Open-gate close guard — red pin for a live near-miss (2026-08-25, task
3baadd19).

A gate-decide click, made through the owner's own authorized remote-assist
session, landed on the wrong control: not the real Approve button, but the
task page's plain "-> done" quick-status pill. That pill PATCHed
status=done with ZERO gate awareness -- it went through immediately,
silently producing a "DONE" task whose green_gate had never actually
passed, bypassing every distinct-actor/human-only safeguard the gate
machinery exists to enforce. Caught only because the backend was
re-checked directly instead of trusting the UI's own "Moved to done"
toast.

Same shape as the existing conductor session gate (task ef81fc15,
test_conductor_session_gate.py): a public choke point (REST PATCH + MCP
task_update alike) refuses a status TRANSITION that would silently defeat
the conductor's own machinery, and names the fix in the refusal. The real
gate-decide path (POST /api/conductor/gate) and the Rewind lever
(POST /api/conductor/rewind, for undoing a wrong decision) are both
untouched -- this only closes the OTHER, ungated door.
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

_PID = "test-open-gate-guard"


@pytest.fixture
def project(tmp_path):
    from prism_service import config as cfg
    original = cfg.PROJECTS_DIR
    cfg.PROJECTS_DIR = tmp_path / "projects"
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    from prism_service import project_context as pc
    pc._contexts.clear()
    yield _PID
    cfg.PROJECTS_DIR = original
    pc._contexts.clear()


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api.tasks import router as tasks_router

    app = FastAPI()
    app.include_router(tasks_router, prefix="/api/tasks")
    return TestClient(app)


def _call(tool_name, arguments=None, project_id=_PID):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(
        handle_tool(tool_name, arguments or {}, project_id=project_id))


def _text(result):
    assert len(result) == 1
    return result[0].text


# ---------------------------------------------------------------------------
# is_open_gate_step -- pure function
# ---------------------------------------------------------------------------


def test_pure_helper_true_for_pending_or_failed_gate_step():
    from prism_service.services.task_service import is_open_gate_step

    assert is_open_gate_step("green_gate", "pending") is True
    assert is_open_gate_step("story_gate", "failed") is True
    assert is_open_gate_step("red_gate", "pending") is True


def test_pure_helper_false_when_gate_passed_or_step_is_not_a_gate():
    from prism_service.services.task_service import is_open_gate_step

    assert is_open_gate_step("green_gate", "passed") is False, (
        "a genuinely-passed gate must not be treated as open"
    )
    assert is_open_gate_step("verify_green_state", "pending") is False, (
        "an agent step (not type=gate) must never be treated as an open gate"
    )
    assert is_open_gate_step("", "none") is False, (
        "a non-conductor task (blank workflow_step) must not be gated"
    )
    assert is_open_gate_step("unknown_step_id", "pending") is False


# ---------------------------------------------------------------------------
# REST PATCH /api/tasks/{id} -- the exact path the incident went through
# ---------------------------------------------------------------------------


def _make_gated_task(project, workflow_step="green_gate", gate_state="pending",
                     proof_type="demo"):
    from prism_service.project_context import get_project
    svc = get_project(project).task_svc
    t = svc.create(title="epic parked at a gate", proof_type=proof_type)
    svc.update(t.id, workflow_step=workflow_step, gate_state=gate_state,
              status="in_progress")
    return t.id


def test_rest_patch_status_done_on_open_gate_is_422(project):
    client = _client()
    tid = _make_gated_task(project)

    patched = client.patch(
        f"/api/tasks/{tid}?project={project}", json={"status": "done"})
    assert patched.status_code == 422, (
        "status=done on a task parked at a pending gate must be refused -- "
        "this is the exact path that silently produced a false-done epic"
    )
    detail = str(patched.json().get("detail", ""))
    assert "gate" in detail.lower()
    # Fix must be NAMED in the error (self-healing 422), matching the
    # session-gate precedent's own convention.
    assert "/api/conductor/gate" in detail or "conductor/rewind" in detail

    got = client.get(f"/api/tasks/{tid}?project={project}").json()
    assert got["task"]["status"] == "in_progress", (
        "a refused PATCH must leave the task's real status untouched"
    )


def test_rest_patch_status_done_on_failed_gate_is_also_422(project):
    client = _client()
    tid = _make_gated_task(project, gate_state="failed")

    patched = client.patch(
        f"/api/tasks/{tid}?project={project}", json={"status": "done"})
    assert patched.status_code == 422


def test_rest_patch_status_done_allowed_once_gate_genuinely_passed(project):
    """The guard must never block the REAL, legitimate close -- once
    gate_state is actually 'passed' (via the real gate_decide path), the
    quick pill still works exactly as before."""
    client = _client()
    tid = _make_gated_task(project, gate_state="passed")

    patched = client.patch(
        f"/api/tasks/{tid}?project={project}", json={"status": "done"})
    assert patched.status_code == 200
    assert patched.json()["task"]["status"] == "done"


def test_rest_patch_status_done_allowed_for_a_non_conductor_task(project):
    """A task with no workflow_step at all (never entered the conductor)
    must not be caught by this guard -- it isn't sitting at a gate."""
    client = _client()
    from prism_service.project_context import get_project
    tid = get_project(project).task_svc.create(title="plain task").id

    patched = client.patch(
        f"/api/tasks/{tid}?project={project}", json={"status": "done"})
    assert patched.status_code == 200


# ---------------------------------------------------------------------------
# MCP task_update -- mirrors the REST guard via the same shared helper
# ---------------------------------------------------------------------------


def test_mcp_task_update_status_done_on_open_gate_structured_error(project):
    tid = _make_gated_task(project)

    out = json.loads(_text(_call(
        "task_update", {"id": tid, "status": "done"})))
    assert "error" in out, (
        "MCP task_update -> done on a task parked at a pending gate must "
        "return a structured error, not silently close it"
    )
    assert "gate" in json.dumps(out).lower()

    from prism_service.project_context import get_project
    assert get_project(project).task_svc.get(tid).status == "in_progress"


def test_mcp_task_update_status_done_allowed_once_gate_passed(project):
    tid = _make_gated_task(project, gate_state="passed")

    out = json.loads(_text(_call(
        "task_update", {"id": tid, "status": "done"})))
    assert out.get("status") == "done"
