"""GET /api/work/graph must answer in bounded time even when a linked
session's transcript I/O is slow or unbounded (the live defect: on the
real dev daemon, GET /api/work/graph?project=prism never returned --
curl -m 15/-m 60 both timed out -- while /api/version answered instantly).

Root cause: work_graph()'s per-node enrichment calls
claude_transcripts.live_spend_for_session (via _task_spend_usd) and
claude_transcripts.live_token_events_for_session once per session per
node, with NO wall-clock bound. Each of those, on a cold cache, walks
EVERY project directory under ~/.claude/projects (440 on the live
instance) and parses whatever transcript files match -- for this project
alone, ~640MB across 358 files, plus hundreds of MB more in worktree
project dirs. A single slow/cold session lookup blocks the whole request;
N linked sessions block it N times over (spend loop, then the session/
token-motion loop calls the same session again).

This test never touches real transcript files -- it monkeypatches the
two transcript entry points to sleep, which is enough to prove the
handler either does or does not bound its own wall-clock time."""

from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# Comfortably above any real budget the fix will set (candidate: 1.5s) and
# comfortably below the artificial per-call sleep this test injects (3s
# per call, 2 calls per session on the slow path today) -- so an unbounded
# handler fails this loudly, and a bounded one passes with real margin.
_ASSERT_UNDER_S = 2.5
_SLEEP_PER_CALL_S = 3.0


def _client(tmp_path, monkeypatch, n_sessions=1):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services import claude_transcripts as ct
    from prism_service.api import work as work_api

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic with a slow transcript corpus")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    session_ids = [f"sess-slow-{i}" for i in range(n_sessions)]
    for sid in session_ids:
        task_svc.link_session(root.id, sid)

    def slow_spend(sid, sp, override_dir=None):
        time.sleep(_SLEEP_PER_CALL_S)
        return {
            "main": {}, "background": {},
            "total": {"usd": 9.0, "tokens": 900, "components": {},
                       "unpriced_tokens": 0, "priced": True},
            "priced": True,
        }

    def slow_events(sid, sp, override_dir=None):
        time.sleep(_SLEEP_PER_CALL_S)
        return [(time.time(), 500)]

    monkeypatch.setattr(ct, "live_spend_for_session", slow_spend)
    monkeypatch.setattr(ct, "live_token_events_for_session", slow_events)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc
    ctx._data_dir = tmp_path

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    return TestClient(app), root.id


def test_graph_answers_promptly_despite_a_slow_transcript_lookup(tmp_path, monkeypatch):
    client, root_id = _client(tmp_path, monkeypatch, n_sessions=1)

    t0 = time.monotonic()
    resp = client.get("/api/work/graph?project=gamify")
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200, resp.text
    assert elapsed < _ASSERT_UNDER_S, (
        f"GET /api/work/graph took {elapsed:.2f}s against a single session "
        f"whose transcript lookups sleep {_SLEEP_PER_CALL_S}s each -- the "
        "handler must bound its own wall-clock time (cap the walk, cache, "
        "or offload with a timeout) rather than block on live_spend_for_"
        "session/live_token_events_for_session; this is the live /live-page "
        "hang (curl -m 60 to /api/work/graph?project=prism never returned)"
    )
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    assert root_id in nodes, f"root task must still appear; got {resp.json()['nodes']!r}"


def test_graph_answers_promptly_with_several_slow_sessions(tmp_path, monkeypatch):
    # The real defect scales with node count AND session count -- prove the
    # bound holds with several slow sessions on one node, not just one.
    client, root_id = _client(tmp_path, monkeypatch, n_sessions=4)

    t0 = time.monotonic()
    resp = client.get("/api/work/graph?project=gamify")
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200, resp.text
    assert elapsed < _ASSERT_UNDER_S, (
        f"GET /api/work/graph took {elapsed:.2f}s against 4 sessions whose "
        f"transcript lookups sleep {_SLEEP_PER_CALL_S}s each (unbounded "
        "today: 4 sessions x 2 call sites x 3s = up to 24s) -- the handler "
        "must not scale with slow-session count; expected it to bound its "
        f"own time under {_ASSERT_UNDER_S}s regardless"
    )
