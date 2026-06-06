"""RED scaffold — task-detail Activity Gantt + stop the synthetic-gate-actor
leak (task ade01b38).

Pins the USER-FACING seams, not unit contracts:

Backend (api/tasks._build_timeline + GET /api/tasks/{id}.timeline):
  * lanes contain ONLY transcript-backed UUID-shaped session ids; non-UUID
    synthetic gate-actor labels (qa-red-gate-*, *-verifier-*) are EXCLUDED
    from lanes — the leak fix.
  * each lane carries session_id, start, end (>=start), duration_s, skills,
    live(=ended_at is None).
  * gates markers derive from history action=='gate_decide' rows with correct
    gate/ts/actor/override/verified(=not override)/proof, and SKIP the
    intermediate REJECTED attempt (verifier=fail w/o override=True), rendering
    only the RESOLVING row.
  * the new field rides the REAL GET /api/tasks/{id} route (not just the
    helper) — asserted through FastAPI TestClient end-to-end.

Frontend (scanned on disk — the render path is the seam):
  * web/src/components/conductor/TaskActivityGantt.tsx exists, exports a
    Timeline type, draws a time axis + per-session bars + gate honesty markers
    (override hollow '!' / verified '✓'), click-to-drill.
  * TaskDetailPage.tsx imports TaskActivityGantt, threads d.timeline into
    state, renders it inside an 'Activity' Card, and filters the Sessions list
    to realSessions (UUID-only) with the count reflecting realSessions.length.

ALL must FAIL today: _build_timeline is absent (no `timeline` field on the
route), the TSX component file does not exist, and TaskDetailPage still renders
every raw session row.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
# .../services/prism-service/tests/integration/<file> -> service root is 3 up.
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"

_REAL_SID = "6f64ed28-1111-2222-3333-444444444444"
_BASE = "2026-06-05T20:"


def _iso(mm: int, ss: int) -> str:
    return f"{_BASE}{mm:02d}:{ss:02d}+00:00"


def _sessions() -> list[dict]:
    """One REAL work session (UUID) + two SYNTHETIC gate actors (non-UUID).

    Mirrors task 3826dac3's real shape: the driving session plus the two gate
    actors the no-self-override guard links. Only the UUID one is a real lane.
    """
    return [
        {
            "session_id": _REAL_SID,
            "started_at": _iso(0, 0),
            "ended_at": _iso(30, 0),
            "duration_s": 1800,
            "tokens_used": 1234,
            "files_read": 5,
            "files_modified": 3,
            "skills_invoked": 7,
        },
        {"session_id": "qa-red-gate-3826dac3", "started_at": None,
         "ended_at": None, "duration_s": 0, "skills_invoked": 0},
        {"session_id": "green-gate-verifier-3826dac3", "started_at": None,
         "ended_at": None, "duration_s": 0, "skills_invoked": 0},
    ]


def _history() -> list[dict]:
    """gate_decide rows: a REJECTED red attempt (verifier=fail, no override)
    that must be SKIPPED, the RESOLVING red gate_decide, and a verified green
    gate_decide carrying a real suite count."""
    return [
        {"id": 1, "action": "created", "timestamp": _iso(0, 0),
         "actor": "", "details": ""},
        {"id": 2, "action": "gate_decide", "timestamp": _iso(5, 0),
         "actor": "qa", "details": "gate=red_gate verifier=fail; trace captured"},
        {"id": 3, "action": "gate_decide", "timestamp": _iso(6, 0),
         "actor": "manual-override",
         "details": ("gate=red_gate verifier=fail; override=True "
                     "override-actor=siegeon; trace captured")},
        {"id": 4, "action": "gate_decide", "timestamp": _iso(25, 0),
         "actor": "qa", "details": "gate=green_gate verifier=pass 47 passed"},
    ]


# ---------------------------------------------------------------------------
# Backend: _build_timeline contract
# ---------------------------------------------------------------------------

def test_lanes_exclude_non_uuid_synthetic_actors():
    """THE LEAK FIX: only the UUID session becomes a lane; the two synthetic
    gate-actor labels never do."""
    from prism_service.api import tasks as T

    tl = T._build_timeline(_history(), _sessions())
    lane_ids = [l["session_id"] for l in tl["lanes"]]
    assert lane_ids == [_REAL_SID], lane_ids
    assert "qa-red-gate-3826dac3" not in lane_ids
    assert "green-gate-verifier-3826dac3" not in lane_ids


def test_lane_carries_expected_fields():
    from prism_service.api import tasks as T

    tl = T._build_timeline(_history(), _sessions())
    (lane,) = tl["lanes"]
    assert lane["session_id"] == _REAL_SID
    assert lane["duration_s"] == 1800
    assert lane["skills"] == 7
    assert lane["live"] is False          # ended_at present
    assert lane["end"] >= lane["start"]   # never inverted
    assert "start" in tl["window"] and "end" in tl["window"]


def test_live_lane_when_ended_at_is_none():
    from prism_service.api import tasks as T

    sess = [{
        "session_id": _REAL_SID, "started_at": _iso(0, 0),
        "ended_at": None, "duration_s": 0, "skills_invoked": 2,
    }]
    (lane,) = T._build_timeline(_history(), sess)["lanes"]
    assert lane["live"] is True


def test_gate_markers_skip_intermediate_rejected_attempt():
    """Row 2 (red_gate verifier=fail, NO override) is the intermediate rejected
    attempt and must NOT render; only the resolving rows do."""
    from prism_service.api import tasks as T

    gates = T._build_timeline(_history(), _sessions())["gates"]
    assert len(gates) == 2, gates  # resolving red (override) + verified green
    by_gate = {g["gate"]: g for g in gates}
    assert set(by_gate) == {"red", "green"}


def test_red_gate_marker_is_override_not_verified():
    from prism_service.api import tasks as T

    gates = T._build_timeline(_history(), _sessions())["gates"]
    red = next(g for g in gates if g["gate"] == "red")
    assert red["override"] is True
    assert red["verified"] is False
    assert red["actor"] in ("siegeon", "manual-override")
    assert red["proof"] == "trace"


def test_green_gate_marker_is_verified_with_suite_proof():
    from prism_service.api import tasks as T

    gates = T._build_timeline(_history(), _sessions())["gates"]
    green = next(g for g in gates if g["gate"] == "green")
    assert green["override"] is False
    assert green["verified"] is True
    assert "47" in green["proof"]  # "suite 47..." — real 2+ digit suite count


# ---------------------------------------------------------------------------
# Backend: the field rides the REAL GET /api/tasks/{id} route (end-to-end)
# ---------------------------------------------------------------------------

class _FakeTaskSvc:
    """Minimal stand-in for TaskService — only what get_task touches."""

    def __init__(self, task, history, sessions):
        self._task, self._history, self._sessions = task, history, sessions

    def get(self, _tid):
        return self._task

    def history(self, _tid):
        return self._history

    def sessions_for_task(self, _tid):
        return self._sessions


class _FakeCtx:
    def __init__(self, task_svc):
        self.task_svc = task_svc
        self.conductor_svc = None  # best-effort phase_progress just skips


def _client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import tasks as tasks_api

    task = {"id": "ade01b38", "title": "t", "status": "in_progress",
            "priority": 0}
    ctx = _FakeCtx(_FakeTaskSvc(task, _history(), _sessions()))
    # _svc and get_task both resolve via api.tasks.get_project.
    monkeypatch.setattr(tasks_api, "get_project", lambda _p: ctx)

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_route_returns_timeline_field_with_filtered_lanes(monkeypatch):
    """The new `timeline` field is on the REAL route response (not just the
    helper), and its lanes exclude the synthetic actors end-to-end."""
    client = _client(monkeypatch)
    r = client.get("/api/tasks/ade01b38?project=proj")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "timeline" in body, "GET /api/tasks/{id} must carry `timeline`"
    tl = body["timeline"]
    lane_ids = [l["session_id"] for l in tl["lanes"]]
    assert lane_ids == [_REAL_SID], lane_ids
    assert any(g["gate"] == "green" and g["verified"] for g in tl["gates"])


# ---------------------------------------------------------------------------
# Frontend: the render path is the seam (scanned on disk)
# ---------------------------------------------------------------------------

def _read(rel: str) -> str:
    return (_WEB_SRC / rel).read_text(encoding="utf-8")


def test_gantt_component_exists_and_exports_timeline_type():
    p = _WEB_SRC / "components" / "conductor" / "TaskActivityGantt.tsx"
    assert p.exists(), f"missing component: {p}"
    src = p.read_text(encoding="utf-8")
    assert "export type Timeline" in src
    # time axis + per-session bars + gate honesty markers + click-to-drill.
    assert "timeline.lanes" in src and "timeline.gates" in src
    assert "override" in src and "verified" in src
    assert "onClick" in src  # click-to-drill


def test_taskdetail_threads_timeline_and_renders_activity_card():
    src = _read("pages/TaskDetailPage.tsx")
    assert "TaskActivityGantt" in src
    assert "import" in src and "TaskActivityGantt" in src
    assert "timeline" in src              # state threaded from d.timeline
    assert "setTimeline" in src
    assert "Activity" in src              # the new Card label
    assert "<TaskActivityGantt" in src


def test_taskdetail_sessions_list_filters_to_real_uuid_sessions():
    """Synthetic metadata-less rows must never render; the count reflects
    realSessions.length, not sessions.length."""
    src = _read("pages/TaskDetailPage.tsx")
    assert "realSessions" in src
    # the count label must read off realSessions, not the raw sessions array.
    assert "Sessions ({realSessions.length})" in src
    assert "realSessions.map" in src
    # a UUID guard must back the filter.
    assert "[0-9a-f]{8}-[0-9a-f]{4}" in src
