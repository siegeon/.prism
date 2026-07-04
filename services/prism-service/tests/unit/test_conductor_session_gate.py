"""Conductor session gate (task ef81fc15) — red scaffold.

Dogfood gap: 5 tasks created via POST /api/tasks and PATCHed to
in_progress rendered as FROZEN conductor tiles — no linked session, so
zero live signals (no tokens, no transcript, no lifecycle animation).
Owner directive: "make sure we can not hand a task to the conductor
without a session."

Pins the gate at every public choke point where a task is handed to the
conductor, plus the REST linking surface that makes compliance possible:

  * REST LINK — POST /api/tasks/{id}/sessions upserts the same
    task_sessions row as the MCP task_link_session verb (idempotent,
    returns the linked list).
  * REST CREATE — TaskCreate accepts session_id (create+link in one
    call); enter_conductor without a session is refused BEFORE the row
    exists (no orphan task).
  * REST PATCH — status->in_progress on a sessionless task is 422 and
    the task stays OUT of the conductor (managed_tasks/intake lane).
  * REST ADVANCE — POST /api/conductor/advance cannot ENTER a
    sessionless task into the workflow.
  * MCP task_update — sessionless in_progress flip returns a structured
    error naming the fix; a resolvable caller session (explicit arg or
    the MCP request context) is AUTO-LINKED so real drives keep working.
  * GRANDFATHER — rows already in_progress without sessions (5 exist on
    the live store) stay readable/patchable; the gate applies to
    transitions only.
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

_PID = "test-session-gate"


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
    from prism_service.api.conductor import router as conductor_router
    from prism_service.api.tasks import router as tasks_router

    app = FastAPI()
    app.include_router(tasks_router, prefix="/api/tasks")
    app.include_router(conductor_router, prefix="/api/conductor")
    return TestClient(app)


def _call(tool_name, arguments=None, project_id=_PID):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(
        handle_tool(tool_name, arguments or {}, project_id=project_id))


def _text(result):
    assert len(result) == 1
    return result[0].text


def _managed_ids(project_id=_PID):
    from prism_service.project_context import get_project
    return {m["id"] for m in get_project(project_id).conductor_svc.managed_tasks()}


# ----------------------------------------------------------------------
# (a) REST PATCH -> in_progress sessionless is refused; task stays out
# ----------------------------------------------------------------------


def test_rest_patch_in_progress_sessionless_is_422(project):
    client = _client()
    r = client.post(f"/api/tasks?project={project}", json={"title": "frozen tile"})
    assert r.status_code == 200
    tid = r.json()["task"]["id"]

    patched = client.patch(
        f"/api/tasks/{tid}?project={project}", json={"status": "in_progress"})
    assert patched.status_code == 422, (
        "sessionless PATCH -> in_progress must be refused — this is the "
        "exact path that produced the 5 frozen conductor tiles"
    )
    assert "session" in str(patched.json().get("detail", "")).lower()
    # Fix must be NAMED in the error (self-healing 422).
    assert "/sessions" in str(patched.json().get("detail", ""))

    # Task untouched and NOT handed to the conductor.
    got = client.get(f"/api/tasks/{tid}?project={project}").json()
    assert got["task"]["status"] == "pending"
    assert tid not in _managed_ids()


# ----------------------------------------------------------------------
# (b) link via REST, then the same PATCH succeeds and conductor sees it
# ----------------------------------------------------------------------


def test_rest_link_then_patch_enters_conductor(project):
    client = _client()
    tid = client.post(
        f"/api/tasks?project={project}", json={"title": "linked tile"},
    ).json()["task"]["id"]

    linked = client.post(
        f"/api/tasks/{tid}/sessions?project={project}",
        json={"session_id": "S-gate"})
    assert linked.status_code == 200
    body = linked.json()
    assert body["linked"] is True
    assert [s["session_id"] for s in body["sessions"]] == ["S-gate"]

    patched = client.patch(
        f"/api/tasks/{tid}?project={project}", json={"status": "in_progress"})
    assert patched.status_code == 200
    assert patched.json()["task"]["status"] == "in_progress"

    # Now it IS conductor-visible (intake lane) with live phase_progress.
    from prism_service.project_context import get_project
    managed = {m["id"]: m for m in
               get_project(project).conductor_svc.managed_tasks()}
    assert tid in managed
    assert managed[tid]["workflow_step"] == "intake"
    assert "phase_progress" in managed[tid]


# ----------------------------------------------------------------------
# (c) TaskCreate with session_id + enter_conductor links in one call
# ----------------------------------------------------------------------


def test_create_with_session_id_and_enter_conductor(project):
    client = _client()
    r = client.post(
        f"/api/tasks?project={project}",
        json={"title": "atomic entry", "session_id": "S-atomic",
              "enter_conductor": True})
    assert r.status_code == 200
    body = r.json()
    tid = body["task"]["id"]
    assert body["advanced"] and body["advanced"].get("ok") is True
    assert any(s["session_id"] == "S-atomic" for s in body.get("sessions", []))

    got = client.get(f"/api/tasks/{tid}?project={project}").json()
    assert any(s["session_id"] == "S-atomic" for s in got["sessions"])
    assert tid in _managed_ids()


def test_create_enter_conductor_sessionless_is_422_and_no_orphan(project):
    client = _client()
    before = len(client.get(f"/api/tasks?project={project}").json()["tasks"])
    r = client.post(
        f"/api/tasks?project={project}",
        json={"title": "sessionless entry", "enter_conductor": True})
    assert r.status_code == 422
    assert "session" in str(r.json().get("detail", "")).lower()
    after = len(client.get(f"/api/tasks?project={project}").json()["tasks"])
    assert after == before, "refused enter_conductor must not orphan a task row"


# ----------------------------------------------------------------------
# (e) REST link endpoint semantics: idempotent, validated
# ----------------------------------------------------------------------


def test_link_endpoint_idempotent_relink(project):
    client = _client()
    tid = client.post(
        f"/api/tasks?project={project}", json={"title": "relink"},
    ).json()["task"]["id"]

    first = client.post(
        f"/api/tasks/{tid}/sessions?project={project}",
        json={"session_id": "S-idem"})
    again = client.post(
        f"/api/tasks/{tid}/sessions?project={project}",
        json={"session_id": "S-idem"})
    assert first.status_code == 200 and again.status_code == 200
    sessions = again.json()["sessions"]
    assert [s["session_id"] for s in sessions] == ["S-idem"], (
        "re-link must be idempotent — one row, not duplicates"
    )
    # started_at survives the re-link (first-observed semantics).
    assert sessions[0]["started_at"] == first.json()["sessions"][0]["started_at"]


def test_link_endpoint_validates_task_and_session(project):
    client = _client()
    missing = client.post(
        f"/api/tasks/no-such-task/sessions?project={project}",
        json={"session_id": "S-x"})
    assert missing.status_code == 404

    tid = client.post(
        f"/api/tasks?project={project}", json={"title": "blank sid"},
    ).json()["task"]["id"]
    blank = client.post(
        f"/api/tasks/{tid}/sessions?project={project}",
        json={"session_id": "   "})
    assert blank.status_code == 422


# ----------------------------------------------------------------------
# REST /api/conductor/advance cannot ENTER a sessionless task
# ----------------------------------------------------------------------


def test_rest_conductor_advance_entry_gated(project):
    client = _client()
    tid = client.post(
        f"/api/tasks?project={project}", json={"title": "spa advance"},
    ).json()["task"]["id"]

    refused = client.post(
        f"/api/conductor/advance?project={project}", json={"task_id": tid})
    assert refused.status_code == 422

    ok = client.post(
        f"/api/conductor/advance?project={project}",
        json={"task_id": tid, "session_id": "S-spa"})
    assert ok.status_code == 200
    assert ok.json().get("ok") is True
    assert tid in _managed_ids()


# ----------------------------------------------------------------------
# (d) MCP task_update: structured error sessionless, auto-link otherwise
# ----------------------------------------------------------------------


def test_mcp_task_update_in_progress_sessionless_structured_error(project):
    t = json.loads(_text(_call("task_create", {"title": "mcp gate"})))
    out = json.loads(_text(_call(
        "task_update", {"id": t["id"], "status": "in_progress"})))
    assert "error" in out, (
        "MCP task_update -> in_progress with NO resolvable session must "
        "return a structured error, not silently hand a sessionless task "
        "to the conductor"
    )
    assert "task_link_session" in json.dumps(out)

    from prism_service.project_context import get_project
    assert get_project(project).task_svc.get(t["id"]).status == "pending"


def test_mcp_task_update_in_progress_with_explicit_session_autolinks(project):
    t = json.loads(_text(_call("task_create", {"title": "mcp explicit"})))
    out = json.loads(_text(_call("task_update", {
        "id": t["id"], "status": "in_progress", "session_id": "S-mcp"})))
    assert out.get("status") == "in_progress"

    from prism_service.project_context import get_project
    linked = get_project(project).task_svc.sessions_for_task(t["id"])
    assert any(s["session_id"] == "S-mcp" for s in linked), (
        "explicit session_id on the in_progress flip must be auto-linked"
    )


def test_mcp_task_update_refuses_phantom_request_handle(project):
    """Over HTTP every MCP request carries a NON-EMPTY request_id — a
    per-request uuid4 minted by the transport that maps to NO transcript.
    The gate must NOT count it as a session: with no explicit arg and no
    real transcript resolvable, the structured refusal fires instead of a
    phantom auto-link (which would clear the gate while the tile stays
    exactly as frozen)."""
    from prism_service.mcp.request_context import (
        PrismRequestContext, use_request_context,
    )

    t = json.loads(_text(_call("task_create", {"title": "mcp ctx"})))
    with use_request_context(PrismRequestContext(
            project_id=_PID, request_id="REQ-PHANTOM-1")):
        out = json.loads(_text(_call(
            "task_update", {"id": t["id"], "status": "in_progress"})))
    assert "error" in out, (
        "a bare request handle satisfied the gate — the refusal branch "
        "must fire when nothing REAL resolves"
    )
    assert "task_link_session" in json.dumps(out)

    from prism_service.project_context import get_project
    assert get_project(project).task_svc.get(t["id"]).status == "pending"
    assert get_project(project).task_svc.sessions_for_task(t["id"]) == [], (
        "the phantom request handle must never be auto-linked"
    )


def test_mcp_task_update_autolinks_real_transcript_session(project, monkeypatch):
    """When the STRICT resolver finds a real transcript session on disk,
    the sessionless in_progress flip auto-links IT (not the request
    handle) and proceeds — live drives with a real transcript never
    stall."""
    from prism_service.mcp import tools as mcp_tools

    monkeypatch.setattr(
        mcp_tools, "_resolve_real_session_id", lambda: "S-real-transcript")

    t = json.loads(_text(_call("task_create", {"title": "mcp real"})))
    out = json.loads(_text(_call(
        "task_update", {"id": t["id"], "status": "in_progress"})))
    assert out.get("status") == "in_progress"

    from prism_service.project_context import get_project
    linked = get_project(project).task_svc.sessions_for_task(t["id"])
    assert any(s["session_id"] == "S-real-transcript" for s in linked)


def test_mcp_conductor_advance_entry_refuses_phantom(project):
    """MCP conductor_advance ENTERING the workflow ('' -> step 0) must not
    be satisfied by the phantom request handle either — same strict rule
    as task_update. With an explicit session_id the entry proceeds."""
    from prism_service.mcp.request_context import (
        PrismRequestContext, use_request_context,
    )
    from prism_service.project_context import get_project

    t = json.loads(_text(_call("task_create", {"title": "mcp advance"})))
    with use_request_context(PrismRequestContext(
            project_id=_PID, request_id="REQ-PHANTOM-2")):
        refused = json.loads(_text(_call(
            "conductor_advance", {"id": t["id"]})))
    assert "error" in refused, (
        "sessionless MCP conductor_advance entered the workflow on a "
        "phantom request handle"
    )
    svc = get_project(project).task_svc
    assert (svc.get(t["id"]).workflow_step or "") == ""
    assert svc.sessions_for_task(t["id"]) == []

    ok = json.loads(_text(_call(
        "conductor_advance", {"id": t["id"], "session_id": "S-entry"})))
    assert ok.get("ok") is True
    assert (svc.get(t["id"]).workflow_step or "") != ""


def test_mcp_conductor_advance_mid_workflow_not_entry_gated(project):
    """Mid-workflow advances are not entry transitions — a task already
    inside the workflow keeps advancing without an explicit session_id
    (the lenient telemetry stamp still applies)."""
    t = json.loads(_text(_call("task_create", {"title": "mcp mid"})))
    entered = json.loads(_text(_call(
        "conductor_advance", {"id": t["id"], "session_id": "S-mid"})))
    assert entered.get("ok") is True

    again = json.loads(_text(_call("conductor_advance", {"id": t["id"]})))
    assert "error" not in again
    assert again.get("ok") is True


# ----------------------------------------------------------------------
# Grandfathering — pre-gate sessionless in_progress rows keep working
# ----------------------------------------------------------------------


def test_grandfathered_sessionless_in_progress_rows_not_bricked(project):
    """The 5 live sessionless in_progress rows predate the gate. Reads,
    conductor views, and NON-transition PATCHes must keep working; the
    gate fires on transitions only."""
    from prism_service.project_context import get_project

    svc = get_project(project).task_svc
    t = svc.create(title="legacy frozen tile")
    svc.update(t.id, status="in_progress")  # internal path — pre-gate row

    client = _client()
    got = client.get(f"/api/tasks/{t.id}?project={project}")
    assert got.status_code == 200
    assert got.json()["sessions"] == []
    assert t.id in _managed_ids()  # still visible (frozen, but not a crash)

    # Non-status PATCH is not gated.
    r = client.patch(
        f"/api/tasks/{t.id}?project={project}", json={"priority": 5})
    assert r.status_code == 200

    # Re-asserting the SAME status is not a transition — not gated.
    same = client.patch(
        f"/api/tasks/{t.id}?project={project}",
        json={"status": "in_progress", "priority": 6})
    assert same.status_code == 200
