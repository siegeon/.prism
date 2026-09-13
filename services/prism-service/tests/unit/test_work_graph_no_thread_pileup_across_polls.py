"""GET /api/work/graph must not accumulate a new background thread for the
SAME slow transcript lookup on every poll (task: live-page transcript-I/O
hang, round 2).

The first fix (test_work_graph_transcript_io_budget.py) bounded each
CALLER'S wait via `_bounded`'s per-call timeout + shared deadline, and that
part genuinely works -- proven there and re-proven here. But it left a gap:
a timed-out call is deliberately left running in the background (the
module comment says so, to warm the cache for next time), and nothing
DEDUPLICATES that background call against a SECOND, THIRD, ... Nth poll for
the exact same (fn, args) while the first is still in flight. The /live
SPA re-fetches this endpoint on every page load/tab, and GraphState's
self-heal path re-fetches again -- so in production this repeats every few
seconds. Each repeat that arrives before the previous background read
finishes submits ANOTHER copy of the same slow read to `_GRAPH_IO_POOL`
(max_workers=16); against real cold, multi-hundred-MB transcripts (minutes
to parse a single file, pure-Python json.loads in a tight loop -- CPU-bound,
not just I/O-bound), enough repeated polls pile up MANY threads all
competing for the GIL, and that contention was observed live: the daemon's
own watchdog (a GET / self-probe, unrelated to this route) started
timing out too (24 stack dumps logged), which only makes sense if
something is starving the WHOLE process, not merely this one route.

This test proves the dedup: N sequential polls for a task linked to ONE
session whose live_spend_for_session sleeps past every poll's own budget
must call the underlying slow function AT MOST ONCE while it is still
running -- never once per poll."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _client(tmp_path, monkeypatch, call_counter, sleep_s):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services import claude_transcripts as ct
    from prism_service.api import work as work_api

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic with one chronically-slow session")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    task_svc.link_session(root.id, "sess-piles-up")

    def slow_spend(sid, sp, override_dir=None):
        call_counter["n"] += 1
        time.sleep(sleep_s)
        return {
            "main": {}, "background": {},
            "total": {"usd": 3.0, "tokens": 300, "components": {},
                       "unpriced_tokens": 0, "priced": True},
            "priced": True,
        }

    def slow_events(sid, sp, override_dir=None):
        time.sleep(sleep_s)
        return [(time.time(), 100)]

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


def test_repeated_polls_share_one_in_flight_call_not_one_each(tmp_path, monkeypatch):
    # The slow call sleeps far longer than the per-request budget, so every
    # individual poll times out and returns fast (proven already) -- what
    # this test adds is: the UNDERLYING slow function must not be re-invoked
    # by every poll while an earlier call for the identical session is still
    # running, or N polls spawn N permanently-abandoned threads.
    #
    # _task_spend_usd's own 5s result cache (_SPEND_CACHE_TTL_S) would mask
    # exactly this defect on a fast test (5 polls easily land inside one
    # 5s window) -- collapsed to near-zero so this test isolates the
    # IN-FLIGHT dedup question from that unrelated, already-correct cache.
    from prism_service.api import work as work_api
    monkeypatch.setattr(work_api, "_SPEND_CACHE_TTL_S", 0.001)
    # Shrink the per-call timeout too, so 5 sequential polls' own wait time
    # stays comfortably under the slow call's sleep duration below -- a
    # dedup'd wait still blocks each poll for up to this long (proven
    # correct: round-1's per-poll bound still holds), it just does not
    # submit a SECOND background thread while doing so.
    monkeypatch.setattr(work_api, "_GRAPH_CALL_TIMEOUT_S", 0.1)

    call_counter = {"n": 0}
    client, root_id = _client(tmp_path, monkeypatch, call_counter, sleep_s=3.0)

    n_polls = 5
    t0 = time.monotonic()
    for _ in range(n_polls):
        resp = client.get("/api/work/graph?project=gamify")
        assert resp.status_code == 200, resp.text
    elapsed = time.monotonic() - t0

    # Each poll still answers promptly (the round-1 fix holds).
    assert elapsed < 2.0, (
        f"{n_polls} sequential polls took {elapsed:.2f}s total -- each poll "
        "must still answer within its own budget"
    )
    assert call_counter["n"] <= 1, (
        f"live_spend_for_session was invoked {call_counter['n']} times "
        f"across {n_polls} polls of the SAME session while the first call "
        "was still sleeping -- expected AT MOST 1: a poll that arrives "
        "while an identical lookup is already in flight must reuse it, "
        "never spawn another background thread. Uncapped, this is how "
        "repeated /live polling against a large, cold transcript corpus "
        "piles up enough CPU-bound background threads to starve the GIL "
        "for the WHOLE process (observed live: the daemon's own watchdog "
        "self-probe against GET / started timing out too)."
    )
