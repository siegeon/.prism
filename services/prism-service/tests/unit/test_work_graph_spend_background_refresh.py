"""GET /api/work/graph's spend_usd never calls into claude_transcripts on
the request path (route-timing pass, owner brief 2026-09-13, final item).

Live symptom: a warm poll cost 624ms whenever the 5s spend cache expired
mid-request -- the OLD design called claude_transcripts.live_spend_for_
session inline, bounded only by a best-effort per-call timeout. The new
design: a low-priority background thread (_spend_refresh_loop, started
lazily by the first request) owns every live_spend_for_session call;
`_task_spend_usd` on the request path only reads `_TASK_SPEND_CACHE` and
registers interest in `_SPEND_WANTED` for the next pass. These tests call
`_spend_refresh_once()` directly for a deterministic single pass instead
of waiting on the real thread's sleep interval."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _ctx(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc
    ctx._data_dir = tmp_path
    return ctx, task_svc


def test_first_call_never_touches_claude_transcripts(tmp_path, monkeypatch):
    from prism_service.api import work as work_api
    from prism_service.services import claude_transcripts as ct

    ctx, task_svc = _ctx(tmp_path)
    t = task_svc.create(title="never-priced-yet")
    task_svc.link_session(t.id, "sess-1")

    calls = []
    monkeypatch.setattr(
        ct, "live_spend_for_session",
        lambda sid, sp, override_dir=None: (calls.append(sid) or {
            "main": {}, "background": {},
            "total": {"usd": 5.0, "tokens": 0, "components": {},
                       "unpriced_tokens": 0, "priced": True},
            "priced": True,
        }))

    usd, stale = work_api._task_spend_usd(
        "spend-bg-test", t.id, source_path="", override_dir="")
    assert usd == 0.0, "a genuine first-ever miss must answer 0.0, not block"
    assert stale is True
    assert calls == [], (
        "the request path must NEVER call live_spend_for_session itself -- "
        f"got {len(calls)} call(s)")


def test_a_refresh_pass_prices_a_previously_wanted_task(tmp_path, monkeypatch):
    from prism_service.api import work as work_api
    from prism_service.services import claude_transcripts as ct

    ctx, task_svc = _ctx(tmp_path)
    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    t = task_svc.create(title="gets priced next pass")
    task_svc.link_session(t.id, "sess-1")

    monkeypatch.setattr(
        ct, "live_spend_for_session",
        lambda sid, sp, override_dir=None: {
            "main": {}, "background": {},
            "total": {"usd": 2.5, "tokens": 0, "components": {},
                       "unpriced_tokens": 0, "priced": True},
            "priced": True,
        })

    # First call: miss, registers interest.
    usd, stale = work_api._task_spend_usd(
        "spend-bg-test2", t.id, source_path="", override_dir="")
    assert (usd, stale) == (0.0, True)

    # One deterministic background pass.
    work_api._spend_refresh_once()

    usd, stale = work_api._task_spend_usd(
        "spend-bg-test2", t.id, source_path="", override_dir="")
    assert usd == 2.5, f"got usd={usd!r}"
    assert stale is False, "freshly refreshed value must not read stale"


def test_stale_flag_true_once_a_cached_value_ages_past_the_threshold(
        tmp_path, monkeypatch):
    from prism_service.api import work as work_api

    ctx, task_svc = _ctx(tmp_path)
    t = task_svc.create(title="aging spend")
    key = f"spend-bg-test3\x00{t.id}"

    monkeypatch.setattr(work_api, "_SPEND_CACHE_TTL_S", 1.0)
    with work_api._TASK_SPEND_LOCK:
        work_api._TASK_SPEND_CACHE[key] = (
            __import__("time").time() - 10.0, 3.0)

    usd, stale = work_api._task_spend_usd(
        "spend-bg-test3", t.id, source_path="", override_dir="")
    assert usd == 3.0, "a stale cached value is still the best known answer"
    assert stale is True


def test_graph_route_never_calls_claude_transcripts_for_spend(tmp_path, monkeypatch):
    """End-to-end: a real GET /api/work/graph poll against a task with a
    linked session must not touch live_spend_for_session at all, even
    though it used to on every cache-cold/expired poll."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api
    from prism_service.services import claude_transcripts as ct

    ctx, task_svc = _ctx(tmp_path)
    root = task_svc.create(title="Root with a linked session")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    task_svc.link_session(root.id, "sess-1")

    calls = []
    monkeypatch.setattr(
        ct, "live_spend_for_session",
        lambda sid, sp, override_dir=None: (calls.append(sid) or {
            "main": {}, "background": {},
            "total": {"usd": 1.0, "tokens": 0, "components": {},
                       "unpriced_tokens": 0, "priced": True},
            "priced": True,
        }))

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    assert calls == [], (
        f"GET /api/work/graph must never call live_spend_for_session on "
        f"the request path; got {len(calls)} call(s)")
    node = {n["id"]: n for n in resp.json()["nodes"]}[root.id]
    assert node["spend_usd"] == 0.0
    assert node["spend_stale"] is True
