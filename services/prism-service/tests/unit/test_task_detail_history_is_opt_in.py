"""GET /api/tasks/{task_id} stops shipping its whole history by default
(task f77d3e94).

Measured baseline: f4043364's detail payload is ~130 KB, almost entirely the
`history` array (30 rows) plus the per-turn-token/spend attach work that
history feeds. The 8-concurrent-request measurement showed the slowest
response at 7.3-7.8s. Mirrors the EXACT precedent already shipped twice in
this repo:
  - api/conductor.py:19-48 GET /state — `include_outcomes: bool = Query(False)`,
    only attached when true (task d5465a25, pinned by
    tests/unit/test_heavy_polls_are_scoped.py).
  - api/version.py + lib/version.ts:83-97 — `?notes=true` opt-in changelog.

FAILS TODAY because:
  - api/tasks.py:356 `get_task` has no `include_history` param at all; it
    always builds and attaches the full `history` array (tasks.py:366-369,
    375) regardless of query string.
  - TaskDetailPage.tsx's `load()` (tsx:924-955) fetches the bare route ONCE
    and reads `history` straight off that payload (tsx:935 `setHistory(d.history
    ?? [])`) — there is no second explicit fetch that opts in to history, so
    simply gating the backend off by default would blank the Timeline card
    (tsx:2219-2222) and the gate audit-detail disclosure (tsx:1756-1761).

Chosen opt-in shape: `include_history=true` on the SAME route, matching
`include_outcomes` exactly (never a separate sub-route) so `timeline` (which
must stay unconditional per test_task_activity_gantt.py's
test_route_returns_timeline_field_with_filtered_lanes) and `sessions` keep
riding the one payload while only the heavy raw `history` rows gate.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _read(rel: str) -> str:
    p = _WEB / rel
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Backend: GET /api/tasks/{id} history opt-in (end-to-end via TestClient,
# mirrors test_task_activity_gantt.py's _client/_FakeTaskSvc pattern).
# ---------------------------------------------------------------------------

def _history_rows(n: int):
    from prism_service.models.task import TaskHistory

    return [
        TaskHistory(
            id=i, task_id="f4043364", actor="qa",
            action="gate_decide" if i % 5 == 0 else "advance",
            details=("gate=green_gate verifier=pass " + ("x" * 400)),
            timestamp=f"2026-08-0{1 + (i % 2)}T00:{i:02d}:00Z",
        )
        for i in range(n)
    ]


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


def _client(monkeypatch, n_history=30):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import tasks as tasks_api

    task = {"id": "f4043364", "title": "t", "status": "in_progress",
            "priority": 0}
    ctx = _FakeCtx(_FakeTaskSvc(task, _history_rows(n_history), []))
    monkeypatch.setattr(tasks_api, "get_project", lambda _p: ctx)

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_default_response_omits_history(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/api/tasks/f4043364", params={"project": "prism"})
    assert r.status_code == 200, r.text
    body = r.json()
    hist = body.get("history")
    assert not hist, (
        "default GET /api/tasks/{id} must OMIT (empty/absent) `history` — "
        f"got {len(hist) if hist else 0} rows")


def test_include_history_opt_in_returns_full_history(monkeypatch):
    client = _client(monkeypatch, n_history=30)
    r = client.get("/api/tasks/f4043364",
                    params={"project": "prism", "include_history": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body.get("history") or []) == 30, (
        "explicit include_history=true opt-in must still return all 30 rows")


def test_default_response_still_carries_timeline_and_sessions(monkeypatch):
    """Pins the neighbouring-suite constraint (test_task_activity_gantt.py's
    test_route_returns_timeline_field_with_filtered_lanes): `timeline` stays
    unconditional in the default response, only raw `history` gates."""
    client = _client(monkeypatch)
    r = client.get("/api/tasks/f4043364", params={"project": "prism"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "timeline" in body, "`timeline` must stay unconditional"
    assert "sessions" in body, "`sessions` must stay unconditional"
    assert "task" in body


def test_default_response_is_far_smaller_than_opt_in(monkeypatch):
    """Proxy for the 130KB -> <40KB shrink: with 30 chunky history rows the
    default payload must be a small fraction of the opt-in payload's size."""
    import json

    client = _client(monkeypatch, n_history=30)
    default_body = client.get(
        "/api/tasks/f4043364", params={"project": "prism"}).json()
    full_body = client.get(
        "/api/tasks/f4043364",
        params={"project": "prism", "include_history": "true"}).json()
    default_bytes = len(json.dumps(default_body))
    full_bytes = len(json.dumps(full_body))
    assert default_bytes * 3 < full_bytes, (
        f"default ({default_bytes}B) should be well under 1/3 of the "
        f"opt-in payload ({full_bytes}B) once history rows are gated")


# ---------------------------------------------------------------------------
# Frontend: TaskDetailPage.tsx issues its OWN explicit opt-in fetch for
# history rather than relying on the default payload (source-reading, per
# the repo convention — no JS test runner; see
# test_conductor_page_animated_cleanup_ui.py / test_task_list_projection.py).
# ---------------------------------------------------------------------------

def test_taskdetail_issues_separate_include_history_fetch():
    src = _read("pages/TaskDetailPage.tsx")
    # The MAIN load() fetch (tsx:927-928) must NOT be the thing populating
    # history any more — it must not carry the opt-in flag itself, proving a
    # SECOND explicit call exists rather than just flipping the one fetch on
    # unconditionally (which would defeat the point of gating the default).
    main_fetch_idx = src.index("await api.get<{ task: Task")
    main_fetch_line_end = src.index("\n", main_fetch_idx)
    main_fetch_call = src[main_fetch_idx:main_fetch_line_end]
    assert "include_history" not in main_fetch_call, (
        "the default task-detail fetch must stay the lean opt-out call; "
        "history must come from a SEPARATE explicit fetch")
    assert "include_history=true" in src, (
        "TaskDetailPage.tsx must issue its own explicit opt-in fetch "
        "(`include_history=true`, mirroring the include_outcomes/notes=true "
        "precedent) to populate `history` for the Timeline card "
        "(tsx:2219-2222) and the gate audit-detail disclosure (tsx:1756-1761)")
    opt_in_idx = src.index("include_history=true")
    # setHistory must be reachable from that same fetch (within a small
    # window — the handler that awaits this call and stores the result).
    window = src[opt_in_idx:opt_in_idx + 600]
    assert "setHistory" in window, (
        "the include_history=true fetch must feed setHistory so the "
        "Timeline/audit-detail surfaces still render")
