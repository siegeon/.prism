"""A task parked by STEP-RETRY EXHAUSTION (task_runner._handle_stall) has
no release path (measured live: 7 of 19 blocked tasks stuck this way,
409'ing against POST /api/conductor/park/release). This pins the fix:
task_runner grows its own release() mirroring dispatch_guard/
resume_actuator, `_park` prefixes its reason going forward, and the route
recognises both the new prefix and the pre-existing unprefixed wording
already sitting on live tasks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PID = "test-step-retry-release"


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


def _legacy_parked_task(project):
    """Reproduces the exact unprefixed wording task_runner._handle_stall
    already wrote to 7 live tasks before this fix."""
    from prism_service.project_context import get_project
    from prism_service.services.task_runner import ATTEMPT_ACTION, SEAT_ID

    svc = get_project(project).task_svc
    t = svc.create(title="stalled task")
    svc.update(t.id, status="in_progress", workflow_step="write_failing_tests")
    for _ in range(3):
        svc.record_history(
            t.id, action=ATTEMPT_ACTION, actor=SEAT_ID,
            details="step=write_failing_tests; advanced=false")
    reason = ("step write_failing_tests did not advance after 3 attempts; "
              "no red test id was named in the last proof")
    svc.update(t.id, status="blocked", blocked_reason=reason)
    return t.id


def test_legacy_step_retry_park_is_releasable(project):
    """FAILS today: the route 409s on this exact wording, which is the
    reason 7 live tasks have no release path at all."""
    tid = _legacy_parked_task(project)
    client = _client()
    r = client.post(f"/api/conductor/park/release?project={project}",
                     json={"task_id": tid, "actor": "owner"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["unparked"] is True
    assert "task_runner" in body["released"]

    from prism_service.project_context import get_project
    task = get_project(project).task_svc.get(tid)
    assert task.status == "in_progress"
    assert task.blocked_reason == ""


def test_release_resets_the_stall_counter_not_just_the_string(project):
    """The behaviour that matters: after release, the step's stall count
    reads 0 again (a fresh mandate), not merely that blocked_reason
    changed. `_stall_count` resets on any 'blocked'->'in_progress'
    transition recorded in history (task_runner.py's _OPERATOR_RESET_RE),
    which release() must produce via a real status update."""
    from prism_service.services import task_runner

    tid = _legacy_parked_task(project)
    from prism_service.project_context import get_project
    svc = get_project(project).task_svc

    assert task_runner._stall_count(svc, tid, "write_failing_tests") == 3

    result = task_runner.release(project, tid, actor="owner")
    assert result["ok"] is True
    assert result["unparked"] is True

    assert task_runner._stall_count(svc, tid, "write_failing_tests") == 0


def test_new_prefixed_step_retry_park_is_releasable(project):
    """_park now writes a `step-retry:` prefix going forward; the route
    must recognise it too."""
    from prism_service.project_context import get_project
    from prism_service.services.task_runner import PARK_PREFIX

    svc = get_project(project).task_svc
    t = svc.create(title="stalled task 2")
    svc.update(t.id, status="blocked",
               blocked_reason=f"{PARK_PREFIX} step implement_tasks did not "
                               f"advance after 3 attempts; no red test id")

    client = _client()
    r = client.post(f"/api/conductor/park/release?project={project}",
                     json={"task_id": t.id})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    task = get_project(project).task_svc.get(t.id)
    assert task.status == "in_progress"


def test_step_retry_release_also_resets_sibling_seat_counters(project):
    """Mirrors task b490fabc: releasing must call ALL THREE seats'
    release() so a sibling seat (dispatch_guard/resume_actuator) already
    at its own ceiling does not immediately re-park the task on the next
    sweep."""
    from prism_service.services import dispatch_guard, resume_actuator, task_runner

    tid = _legacy_parked_task(project)
    calls = {"dispatch_guard": [], "resume_actuator": [], "task_runner": []}

    def _spy(name, real):
        def _inner(proj, task_id, actor="human"):
            calls[name].append((proj, task_id, actor))
            return real(proj, task_id, actor=actor)
        return _inner

    import pytest as _pytest
    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(dispatch_guard, "release",
                        _spy("dispatch_guard", dispatch_guard.release))
    monkeypatch.setattr(resume_actuator, "release",
                        _spy("resume_actuator", resume_actuator.release))
    monkeypatch.setattr(task_runner, "release",
                        _spy("task_runner", task_runner.release))
    try:
        client = _client()
        r = client.post(f"/api/conductor/park/release?project={project}",
                         json={"task_id": tid})
        assert r.status_code == 200, r.text
    finally:
        monkeypatch.undo()

    assert calls["dispatch_guard"] == [(project, tid, "human")]
    assert calls["resume_actuator"] == [(project, tid, "human")]
    assert calls["task_runner"] == [(project, tid, "human")]
