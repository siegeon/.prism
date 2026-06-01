"""RED scaffold — phase_progress field for the animated SDLC progress bar
(task a5e0d9f5 — Animate Conductor Tasks in the PRISM Web UI).

The segmented SDLC progress bar (ConductorPage tiles + TaskDetailPage header)
needs a server-computed `phase_progress` blend so the current-segment fill can
tween smoothly between 5s polls instead of snapping at each step advance. This
field is NET-NEW: today nothing computes it and neither API payload carries it.

These pin the REAL user-facing seams end-to-end (not a service method in
isolation — that exact gap has shipped false-greens):

  Seam A (compute) — ConductorService.phase_progress(task_id) returns the
    documented shape {pct, basis, in_step_s, typical_s, children_done,
    children_total, tokens_since_step}.
  Seam B (baseline pct) — with no child tasks, pct = min(0.95, in_step_s /
    typical_s) from MEDIAN step history, basis='time', and the 0.95 ceiling
    holds even when in_step_s far exceeds typical_s.
  Seam C (children override) — when child tasks exist (parent_id == task),
    pct is the exact children_done/children_total ratio and basis='children',
    OVERRIDING the time baseline.
  Seam D (conductor /state API) — GET /api/conductor/state managed_tasks
    entries each carry a phase_progress object with pct (reachable through the
    REAL router, not just the method).
  Seam E (task detail API) — GET /api/tasks/{id} returns phase_progress on a
    conductor-managed task object.

ALL FAIL today: ConductorService has no phase_progress method, managed_tasks()
entries lack the key, and neither API route surfaces it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


_SHAPE_KEYS = {
    "pct", "basis", "in_step_s", "typical_s",
    "children_done", "children_total", "tokens_since_step",
}


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"),
        enable_engine=False,
        task_svc=task_svc,
    )
    return task_svc, cond


def _managed(task_svc, cond, title="animate"):
    """Create a task and walk it onto the workflow so managed_tasks() sees it."""
    t = task_svc.create(title=title)
    cond.advance_task(t.id)  # '' -> step 0, workflow_step now non-empty
    return task_svc.get(t.id)


# ----------------------------------------------------------------------
# Seam A — the method exists and returns the documented shape
# ----------------------------------------------------------------------

def test_phase_progress_returns_documented_shape(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = _managed(task_svc, cond)
    assert hasattr(cond, "phase_progress"), \
        "ConductorService must expose phase_progress(task_id)"
    pp = cond.phase_progress(t.id)
    assert isinstance(pp, dict)
    assert _SHAPE_KEYS.issubset(pp.keys()), \
        f"phase_progress shape missing keys: {_SHAPE_KEYS - set(pp.keys())}"
    assert 0.0 <= pp["pct"] <= 1.0


# ----------------------------------------------------------------------
# Seam B — time baseline pct = min(0.95, in_step_s / typical_s)
# ----------------------------------------------------------------------

def test_phase_progress_time_basis_caps_at_0_95(tmp_path):
    """With no children, pct is the time ratio capped at 0.95 — even when the
    task has dwelt far longer than the typical step duration."""
    task_svc, cond = _services(tmp_path)
    t = _managed(task_svc, cond)
    pp = cond.phase_progress(t.id)
    assert pp["basis"] == "time", "no-children progress must be time-based"
    assert pp["children_total"] == 0
    assert pp["pct"] <= 0.95, "time-basis pct must never exceed the 0.95 ceiling"
    # in_step_s comes off the last advance row, so it is a real (>=0) duration.
    assert pp["in_step_s"] >= 0.0
    assert pp["typical_s"] > 0.0, "typical_s must fall back to a positive default"


# ----------------------------------------------------------------------
# Seam C — child tasks OVERRIDE the time baseline with an exact ratio
# ----------------------------------------------------------------------

def test_phase_progress_children_override_time_basis(tmp_path):
    """When parent_id == this task, pct is the exact done/total child ratio
    and basis flips to 'children', overriding the time baseline."""
    task_svc, cond = _services(tmp_path)
    parent = _managed(task_svc, cond, title="parent")

    c1 = task_svc.create(title="child-1")
    c2 = task_svc.create(title="child-2")
    c3 = task_svc.create(title="child-3")
    for c in (c1, c2, c3):
        task_svc.update(c.id, parent_id=parent.id)
    task_svc.update(c1.id, status="done")

    pp = cond.phase_progress(parent.id)
    assert pp["basis"] == "children", \
        "child tasks must override the time baseline"
    assert pp["children_total"] == 3
    assert pp["children_done"] == 1
    assert pp["pct"] == pytest.approx(1 / 3, abs=1e-6), \
        "pct must be the exact children_done/children_total ratio"


# ----------------------------------------------------------------------
# Seam D — conductor /state API surfaces phase_progress per managed task
# ----------------------------------------------------------------------

def _isolated_ctx(tmp_path, monkeypatch):
    """Bind the API routers' get_project to a context whose task_svc and
    conductor_svc share one tasks.db (the real wiring)."""
    task_svc, cond = _services(tmp_path)

    class _Ctx:
        task_svc = None
        conductor_svc = None

    ctx = _Ctx()
    ctx.task_svc = task_svc
    ctx.conductor_svc = cond
    return ctx, task_svc, cond


def test_conductor_state_api_carries_phase_progress(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import conductor as conductor_api

    ctx, task_svc, cond = _isolated_ctx(tmp_path, monkeypatch)
    _managed(task_svc, cond, title="board tile")

    monkeypatch.setattr(conductor_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(conductor_api.router, prefix="/api/conductor")
    client = TestClient(app)

    r = client.get("/api/conductor/state")
    assert r.status_code == 200, r.text
    managed = r.json()["managed_tasks"]
    assert managed, "the managed task must appear on the conductor /state payload"
    tile = managed[0]
    assert "phase_progress" in tile, \
        "each managed_tasks entry must carry a phase_progress object"
    assert "pct" in tile["phase_progress"]


# ----------------------------------------------------------------------
# Seam E — task detail API surfaces phase_progress on the task object
# ----------------------------------------------------------------------

def test_task_detail_api_carries_phase_progress(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api

    ctx, task_svc, cond = _isolated_ctx(tmp_path, monkeypatch)
    t = _managed(task_svc, cond, title="detail header")

    monkeypatch.setattr(tasks_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    client = TestClient(app)

    r = client.get(f"/api/tasks/{t.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "phase_progress" in body, \
        "GET /api/tasks/{id} must surface phase_progress for the detail header bar"
    assert "pct" in body["phase_progress"]
