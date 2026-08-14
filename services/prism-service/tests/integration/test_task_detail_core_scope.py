"""GET /api/tasks/{id}?scope=core skips every transcript-parsing attach.

Owner 2026-08-13: "clicking on a task still does not instant load". Measured:
the full detail assembly took ~32s COLD (0.8s warm) because the per-turn
token, step-token, spend and timeline attaches all parse linked-session
transcript JSONL, and their caches are in-process — empty after every daemon
bounce, which dev doctrine performs on every code change. phase_progress
reads the same live-token paths.

The fix is a scope=core fast path (sqlite + file-stat only) that the SPA
paints from immediately, fetching scope=full right behind it. This suite
pins BOTH halves:

  * scope=core answers WITHOUT calling any transcript-parsing helper and
    without the heavy fields; task/history/sessions still ride it.
  * the default (scope=full) still calls them and carries the fields —
    the panels must not silently die.
  * TaskDetailPage.tsx (scanned on disk — the SPA has no JS runner) loads
    core first, then full, and never clobbers loaded panels on a core merge.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"


class _FakeTaskSvc:
    def __init__(self, task):
        self._task = task

    def get(self, _tid):
        return self._task

    def history(self, _tid):
        return []

    def sessions_for_task(self, _tid):
        return [{"session_id": "6f64ed28-1111-2222-3333-444444444444"}]


class _FakeCtx:
    def __init__(self, task_svc):
        self.task_svc = task_svc
        self.conductor_svc = None  # best-effort phase_progress just skips


def _client(monkeypatch, called):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import tasks as tasks_api

    task = {"id": "t1", "title": "t", "status": "in_progress", "priority": 0}
    ctx = _FakeCtx(_FakeTaskSvc(task))
    monkeypatch.setattr(tasks_api, "get_project", lambda _p: ctx)

    # Tripwires on every transcript-parsing helper the route touches: record
    # the call instead of parsing, so the assertion is about the SEAM (was
    # transcript work requested at all), not about parse output.
    monkeypatch.setattr(
        tasks_api, "_attach_turn_tokens",
        lambda *a, **k: called.append("turn_tokens"))
    monkeypatch.setattr(
        tasks_api, "_step_token_totals",
        lambda *a, **k: (called.append("step_tokens"), {})[1])
    monkeypatch.setattr(
        tasks_api, "_attach_spend",
        lambda *a, **k: called.append("spend"))
    monkeypatch.setattr(
        tasks_api, "_build_timeline",
        lambda *a, **k: (called.append("timeline"), {"lanes": [], "gates": []})[1])

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_scope_core_never_touches_transcript_helpers(monkeypatch):
    called: list[str] = []
    client = _client(monkeypatch, called)
    r = client.get("/api/tasks/t1?project=proj&scope=core")
    assert r.status_code == 200, r.text
    assert called == [], (
        f"scope=core ran transcript-parsing helpers {called} — the whole "
        "point of the fast path is that a cold cache costs nothing here")
    body = r.json()
    # the cheap spine still rides core
    assert body["task"]["id"] == "t1"
    assert "history" in body and "sessions" in body
    # the heavy fields do not
    for heavy in ("timeline", "spend", "step_tokens", "phase_progress"):
        assert heavy not in body, f"scope=core must omit `{heavy}`"


def test_default_scope_still_assembles_the_heavy_panels(monkeypatch):
    called: list[str] = []
    client = _client(monkeypatch, called)
    r = client.get("/api/tasks/t1?project=proj")
    assert r.status_code == 200, r.text
    assert set(called) == {"turn_tokens", "step_tokens", "spend", "timeline"}, (
        f"default scope must keep every panel attach; ran only {called}")
    body = r.json()
    assert "timeline" in body and "step_tokens" in body


def test_taskdetail_page_paints_core_then_fills_full():
    src = (_WEB_SRC / "pages" / "TaskDetailPage.tsx").read_text(
        encoding="utf-8")
    # the mount effect loads core first, then full, in that order
    assert 'await load("core"); await load("full")' in src, (
        "TaskDetailPage must paint from scope=core before fetching the "
        "transcript-heavy full payload")
    # the fetch actually threads the scope through to the API
    assert 'scope === "core" ? "&scope=core"' in src
    # a core merge must not clobber already-loaded heavy panels: the setter
    # falls back to prev state for fields the core response omits
    assert "prev?.spend" in src and "prev?.phase_progress" in src
