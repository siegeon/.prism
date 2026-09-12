"""POST /api/conductor/park/release (task b490fabc follow-up).

Two governance seats park a task `blocked` with a prefixed
`blocked_reason`: `resume_actuator` (`resume-actuator:`) and
`dispatch_guard` (`dispatch-guard:`, "parked for a person"). Only
`resume_actuator`'s had an HTTP release route (`/resume/release`) — a
task parked by `dispatch_guard` had NO person-facing action able to lift
it. This pins the new route that reads the task's own `blocked_reason`
and dispatches to the matching module's `release()`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PID = "test-park-release"


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

    app = FastAPI()
    app.include_router(conductor_router, prefix="/api/conductor")
    return TestClient(app)


def _parked_task(project, prefix, reason_tail="parked for a person"):
    from prism_service.project_context import get_project

    svc = get_project(project).task_svc
    t = svc.create(title="parked task")
    svc.update(t.id, status="blocked",
               blocked_reason=f"{prefix} {reason_tail}")
    return t.id


def test_dispatch_guard_park_releases_via_dispatch_guard(project, monkeypatch):
    from prism_service.services import dispatch_guard

    tid = _parked_task(project, "dispatch-guard:")
    calls = []
    real_release = dispatch_guard.release

    def _spy(proj, task_id, actor="human"):
        calls.append((proj, task_id, actor))
        return real_release(proj, task_id, actor=actor)

    monkeypatch.setattr(dispatch_guard, "release", _spy)

    client = _client()
    r = client.post(f"/api/conductor/park/release?project={project}",
                     json={"task_id": tid, "actor": "owner"})
    assert r.status_code == 200
    body = r.json()
    assert body["module"] == "dispatch_guard"
    assert body["ok"] is True
    assert body["unparked"] is True
    assert calls == [(project, tid, "owner")]

    from prism_service.project_context import get_project
    task = get_project(project).task_svc.get(tid)
    assert task.status == "in_progress"
    assert task.blocked_reason == ""


def test_resume_actuator_park_releases_via_resume_actuator(project, monkeypatch):
    from prism_service.services import resume_actuator

    tid = _parked_task(project, "resume-actuator:",
                        "at the ceiling of 12 dispatches")
    calls = []
    real_release = resume_actuator.release

    def _spy(proj, task_id, actor="human"):
        calls.append((proj, task_id, actor))
        return real_release(proj, task_id, actor=actor)

    monkeypatch.setattr(resume_actuator, "release", _spy)

    client = _client()
    r = client.post(f"/api/conductor/park/release?project={project}",
                     json={"task_id": tid})
    assert r.status_code == 200
    body = r.json()
    assert body["module"] == "resume_actuator"
    assert body["ok"] is True
    assert body["unparked"] is True
    assert calls == [(project, tid, "human")]

    from prism_service.project_context import get_project
    task = get_project(project).task_svc.get(tid)
    assert task.status == "in_progress"


def test_non_governance_blocked_reason_is_409_and_nothing_called(
        project, monkeypatch):
    from prism_service.services import dispatch_guard, resume_actuator
    from prism_service.project_context import get_project

    svc = get_project(project).task_svc
    t = svc.create(title="genuinely blocked")
    svc.update(t.id, status="blocked",
               blocked_reason="waiting on a dependency task")

    dg_calls = []
    ra_calls = []
    monkeypatch.setattr(dispatch_guard, "release",
                         lambda *a, **k: dg_calls.append((a, k)))
    monkeypatch.setattr(resume_actuator, "release",
                         lambda *a, **k: ra_calls.append((a, k)))

    client = _client()
    r = client.post(f"/api/conductor/park/release?project={project}",
                     json={"task_id": t.id})
    assert r.status_code == 409
    assert "not a governance park" in str(r.json().get("detail", "")).lower()
    assert dg_calls == [] and ra_calls == []

    task = svc.get(t.id)
    assert task.status == "blocked"
    assert task.blocked_reason == "waiting on a dependency task"


def test_missing_task_id_is_422(project):
    client = _client()
    r = client.post(f"/api/conductor/park/release?project={project}", json={})
    assert r.status_code == 422
