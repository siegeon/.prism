"""Unknown ?project= must not mint phantom projects (stress finding d37193da).

Any /api/* request naming an unknown project silently created a ~1.1MB
project dir - including plain GETs - because project_context.get_project()
creates on miss and understand_engine routes even READS of
understand_state.json through the creating config.project_data_dir().
The same read path is how the pytest suite leaked test_*0 project dirs
into the shared data dir (ConductorService._project_source_path derives a
"project id" from a tmp scores.db path and reads state for it).

Contract pinned here:
  * reads/writes against an unknown project -> HTTP 404 "unknown project
    '<p>'" and NOTHING persists (AC-1, AC-2, AC-3);
  * the missing-param -> 'default' fallback keeps auto-creating (AC-4);
  * creation stays explicit and working: POST /api/projects (AC-5) and
    MCP project_onboard (AC-7);
  * conductor read probes for a non-project id mint nothing (AC-6, the
    pytest-leak repro).

Assertions run against the EFFECTIVE prism_service.config.PROJECTS_DIR
(module-level, frozen at first import) - not a monkeypatched env var -
so the no-dir-minted checks observe the same store the routes write to.
"""

from __future__ import annotations

import uuid

import pytest


def _projects_dir():
    from prism_service.config import PROJECTS_DIR
    return PROJECTS_DIR


def _fresh_name(prefix: str = "phantom-probe") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from prism_service.main import app

    return TestClient(app)


def _cleanup_project(client, name: str) -> None:
    """Best-effort removal of a project a test deliberately created."""
    try:
        client.delete(f"/api/projects/{name}")
    except Exception:
        pass


# ----------------------------------------------------------------------
# AC-1 / AC-2 / AC-3 - unknown project -> 404, nothing persists
# ----------------------------------------------------------------------


def test_get_tasks_unknown_project_404_no_dir(client):
    """AC-1: the filed repro - a mere GET minted a project dir + 200."""
    name = _fresh_name()
    r = client.get("/api/tasks", params={"project": name})
    assert r.status_code == 404, (
        f"GET /api/tasks?project={name} must 404, got {r.status_code}: "
        f"{r.text[:200]}"
    )
    detail = str(r.json().get("detail", ""))
    assert "unknown project" in detail, detail
    assert name in detail, detail
    assert not (_projects_dir() / name).exists(), (
        "a mere GET minted a project dir on disk"
    )
    listed = client.get("/api/projects").json()["projects"]
    assert name not in listed, "phantom project appeared in /api/projects"


def test_post_tasks_unknown_project_404_no_dir(client):
    """AC-2: a write to an unknown project is a loud 404, not a silent
    task-into-phantom-board loss."""
    name = _fresh_name()
    r = client.post(
        "/api/tasks", params={"project": name}, json={"title": "x"},
    )
    assert r.status_code == 404, (
        f"POST /api/tasks?project={name} must 404, got {r.status_code}: "
        f"{r.text[:200]}"
    )
    detail = str(r.json().get("detail", ""))
    assert "unknown project" in detail and name in detail, detail
    assert not (_projects_dir() / name).exists(), (
        "a rejected write still minted a project dir"
    )


def test_get_conductor_state_unknown_project_404_no_dir(client):
    """AC-3: another project-scoped read route shares the contract."""
    name = _fresh_name()
    r = client.get("/api/conductor/state", params={"project": name})
    assert r.status_code == 404, (
        f"GET /api/conductor/state?project={name} must 404, got "
        f"{r.status_code}: {r.text[:200]}"
    )
    assert "unknown project" in str(r.json().get("detail", ""))
    assert not (_projects_dir() / name).exists(), (
        "a conductor read minted a project dir"
    )


# ----------------------------------------------------------------------
# AC-4 - the long-standing missing-param -> 'default' fallback stays
# ----------------------------------------------------------------------


def test_default_fallback_still_creates(client):
    """AC-4: no project param -> the task lands in 'default' (which is
    allowed to auto-create; it is the documented implicit fallback)."""
    r = client.post("/api/tasks", json={"title": "default-fallback probe"})
    assert r.status_code == 200, r.text
    tid = r.json()["task"]["id"]
    r = client.get(f"/api/tasks/{tid}", params={"project": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["task"]["title"] == "default-fallback probe"


# ----------------------------------------------------------------------
# AC-5 / AC-7 - creation stays explicit AND working
# ----------------------------------------------------------------------


def test_explicit_create_then_read(client):
    """AC-5: POST /api/projects is the explicit creation affordance; the
    project is usable immediately afterwards."""
    name = _fresh_name("explicit-create")
    try:
        r = client.post("/api/projects", json={"name": name})
        assert r.status_code == 200, r.text
        assert r.json()["created"] is True
        r = client.get("/api/tasks", params={"project": name})
        assert r.status_code == 200, (
            f"explicitly created project must be readable: {r.text[:200]}"
        )
        assert r.json()["tasks"] == []
    finally:
        _cleanup_project(client, name)


def test_mcp_project_onboard_still_creates(client):
    """AC-7: MCP project_onboard against a fresh project id is a
    legitimate creation path and must keep working."""
    from prism_service.mcp import tools as mcp_tools

    name = _fresh_name("onboard-probe")
    try:
        out = mcp_tools._dispatch_tool(
            "project_onboard",
            {"project_name": name},
            project_id=name,
        )
        text = "".join(getattr(c, "text", "") or "" for c in out)
        assert "error" not in text.lower() or "onboard" in text.lower(), text
        assert (_projects_dir() / name).is_dir(), (
            "project_onboard must create the project dir"
        )
    finally:
        _cleanup_project(client, name)


# ----------------------------------------------------------------------
# AC-6 - the pytest-leak repro: conductor read probes mint nothing
# ----------------------------------------------------------------------


def test_conductor_probe_mints_no_project_dir(tmp_path):
    """AC-6: ConductorService resolves a 'project id' from its scores.db
    parent dir name and reads understand_state.json for it. That READ
    must not mkdir under the real PROJECTS_DIR - this exact chain leaked
    8 test_*0 phantom project dirs from the pytest suite."""
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService

    # Unique dir name -> the derived "project id" is unique per run, so a
    # phantom leaked by an EARLIER (pre-fix) run can never mask a pass or
    # fail this assertion spuriously.
    probe = tmp_path / _fresh_name("leakprobe")
    probe.mkdir()
    task_svc = TaskService(str(probe / "tasks.db"))
    cond = ConductorService(
        str(probe / "scores.db"),
        enable_engine=False,
        task_svc=task_svc,
    )
    cond._project_source_path()
    cond._project_override_dir()
    leaked = _projects_dir() / probe.name
    assert not leaked.exists(), (
        f"read probe minted {leaked} - reads must never create project dirs"
    )
