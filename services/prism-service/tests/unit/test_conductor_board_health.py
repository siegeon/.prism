"""RED scaffold — cross-task board "reorient" signal (task 77be27a1, GAP-5).

GoalBuddy GAP-5: compose the existing PER-TASK advisory '⚠' notes into ONE
board-level signal, mirroring goalbuddy state.yaml max_consecutive_tiny_tasks:2.

board_health(tasks) -> {consecutive_low_value:int, reorient:bool, reason:str}
scans DONE ROOT tasks (status=='done', parent_id=='') newest-first by
completed_at and counts the LEADING run of LOW-VALUE completions; reorient
flips True when that leading run >= 2.

"Low-value" uses only reliable signals (NOT files_modified — broken
attribution): the done task's gate_reason carries the advisory '⚠' marker
(busywork/oracle/misfire/slice-only), OR is_weak_proof(completion_proof).

These tests pin BOTH the pure helper contract AND the user-facing seam:
the GET /api/conductor/state response must carry board_health additively.
They FAIL today (helper + API field don't exist yet).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _task(*, status="done", parent_id="", gate_reason="", completion_proof="",
          completed_at="2026-06-07T00:00:00+00:00"):
    """A synthetic task row carrying just the fields board_health reads."""
    return SimpleNamespace(
        status=status,
        parent_id=parent_id,
        gate_reason=gate_reason,
        completion_proof=completion_proof,
        completed_at=completed_at,
    )


# A strong (clean) done task: real proof, no advisory marker -> NOT low-value.
def _strong(completed_at):
    return _task(gate_reason="verified (532 tests pass): demo recorded",
                 completion_proof="532 tests pass + demo at /x.mp4",
                 completed_at=completed_at)


# A low-value done task via the '⚠' advisory marker in gate_reason.
def _weak_marker(completed_at):
    return _task(
        gate_reason="manual override: shipped  ⚠ busywork risk: 7 file-change(s)",
        completion_proof="some words",
        completed_at=completed_at)


# A low-value done task via is_weak_proof(completion_proof) (placeholder proof).
def _weak_proof(completed_at):
    return _task(gate_reason="manual override: shipped",
                 completion_proof="tbd", completed_at=completed_at)


# ---------------------------------------------------------------------------
# 0 low-value => no reorient.
# ---------------------------------------------------------------------------
def test_zero_low_value_no_reorient():
    from prism_service.services.conductor_service import board_health
    out = board_health([
        _strong("2026-06-07T03:00:00+00:00"),
        _strong("2026-06-07T02:00:00+00:00"),
        _strong("2026-06-07T01:00:00+00:00"),
    ])
    assert out["consecutive_low_value"] == 0, out
    assert out["reorient"] is False, out


# ---------------------------------------------------------------------------
# 1 leading low-value => still no reorient (threshold is >= 2).
# ---------------------------------------------------------------------------
def test_single_low_value_no_reorient():
    from prism_service.services.conductor_service import board_health
    out = board_health([
        _weak_marker("2026-06-07T03:00:00+00:00"),  # newest, low-value
        _strong("2026-06-07T02:00:00+00:00"),
        _strong("2026-06-07T01:00:00+00:00"),
    ])
    assert out["consecutive_low_value"] == 1, out
    assert out["reorient"] is False, out


# ---------------------------------------------------------------------------
# 2+ leading low-value => reorient True with a non-empty reason.
# ---------------------------------------------------------------------------
def test_two_leading_low_value_reorients():
    from prism_service.services.conductor_service import board_health
    out = board_health([
        _weak_marker("2026-06-07T03:00:00+00:00"),  # newest, low-value
        _weak_proof("2026-06-07T02:00:00+00:00"),   # low-value
        _strong("2026-06-07T01:00:00+00:00"),       # breaks (but run already 2)
    ])
    assert out["consecutive_low_value"] == 2, out
    assert out["reorient"] is True, out
    assert out["reason"].strip(), out


def test_three_leading_low_value_counts_full_run():
    from prism_service.services.conductor_service import board_health
    out = board_health([
        _weak_marker("2026-06-07T03:00:00+00:00"),
        _weak_proof("2026-06-07T02:00:00+00:00"),
        _weak_marker("2026-06-07T01:00:00+00:00"),
    ])
    assert out["consecutive_low_value"] == 3, out
    assert out["reorient"] is True, out


# ---------------------------------------------------------------------------
# A STRONG (clean) task at the HEAD of the newest-first run breaks the count
# even when older tasks are low-value.
# ---------------------------------------------------------------------------
def test_strong_proof_at_head_breaks_run():
    from prism_service.services.conductor_service import board_health
    out = board_health([
        _strong("2026-06-07T03:00:00+00:00"),       # newest, clean -> breaks
        _weak_marker("2026-06-07T02:00:00+00:00"),
        _weak_proof("2026-06-07T01:00:00+00:00"),
    ])
    assert out["consecutive_low_value"] == 0, out
    assert out["reorient"] is False, out


# ---------------------------------------------------------------------------
# Only DONE ROOT tasks count: cancelled/in_progress/pending and non-root
# (parent_id != '') tasks are excluded entirely from the scan.
# ---------------------------------------------------------------------------
def test_non_done_and_non_root_excluded():
    from prism_service.services.conductor_service import board_health
    out = board_health([
        # newest two are NOT eligible -> ignored, not counted, not a break
        _task(status="in_progress", gate_reason="  ⚠ busywork",
              completed_at="2026-06-07T05:00:00+00:00"),
        _task(status="cancelled", completion_proof="tbd",
              completed_at="2026-06-07T04:30:00+00:00"),
        _task(parent_id="some-parent", completion_proof="tbd",
              completed_at="2026-06-07T04:00:00+00:00"),
        # the two eligible done root tasks ARE both low-value
        _weak_marker("2026-06-07T03:00:00+00:00"),
        _weak_proof("2026-06-07T02:00:00+00:00"),
    ])
    assert out["consecutive_low_value"] == 2, out
    assert out["reorient"] is True, out


# ===========================================================================
# USER-FACING SEAM — GET /api/conductor/state must carry board_health
# ADDITIVELY (existing managed_tasks/step_buckets keys unchanged). A pure
# helper that's never wired into the route is DEAD; this pins the route.
# ===========================================================================


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import conductor as conductor_api
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)

    class _Ctx:
        conductor_svc = cond

    monkeypatch.setattr(conductor_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(conductor_api.router, prefix="/api/conductor")
    return TestClient(app), task_svc, cond


def _done_low_value(task_svc, title):
    """A done ROOT task whose gate_reason carries the '⚠' advisory marker."""
    t = task_svc.create(title=title)
    task_svc.update(
        t.id, status="done", workflow_step="green_gate",
        gate_reason="manual override: shipped  ⚠ busywork risk: 5 file-change(s)",
        completion_proof="tbd")
    return t


def test_api_state_carries_board_health_additively(tmp_path, monkeypatch):
    client, task_svc, cond = _client(tmp_path, monkeypatch)
    # Two consecutive low-value done root completions -> reorient.
    _done_low_value(task_svc, "tiny slice 1")
    _done_low_value(task_svc, "tiny slice 2")

    resp = client.get("/api/conductor/state", params={"project": "prism"})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Additive: the existing keys are still present.
    assert "managed_tasks" in body, body.keys()
    assert "step_buckets" in body, body.keys()

    # The new field is present and reflects the reorient signal end-to-end.
    assert "board_health" in body, body.keys()
    bh = body["board_health"]
    assert bh["reorient"] is True, bh
    assert bh["consecutive_low_value"] >= 2, bh
    assert bh["reason"].strip(), bh


def test_api_state_board_health_silent_when_healthy(tmp_path, monkeypatch):
    client, task_svc, cond = _client(tmp_path, monkeypatch)
    # One strong done root task -> no reorient.
    t = task_svc.create(title="clean slice")
    task_svc.update(
        t.id, status="done", workflow_step="green_gate",
        gate_reason="verified (532 tests pass): demo recorded",
        completion_proof="532 tests pass + demo at /x.mp4")

    resp = client.get("/api/conductor/state", params={"project": "prism"})
    assert resp.status_code == 200, resp.text
    bh = resp.json()["board_health"]
    assert bh["reorient"] is False, bh
