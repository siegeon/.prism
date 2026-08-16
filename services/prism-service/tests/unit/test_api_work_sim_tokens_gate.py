"""POST /api/work/sim-tokens -- dev-only door, 404 unless PRISM_DEV_SIM=1
(gamify walking skeleton).

E:\\gamify-lab\\sim\\drive_sim.py needs a way to fabricate tokens.turn
events (there is no real transcript in the sim) without giving every
production instance a bus-injection door. The route must be structurally
absent (404, not 403 -- reads as "does not exist") on any instance that
hasn't opted in via PRISM_DEV_SIM=1, and must actually publish onto the
real bus once opted in.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    return TestClient(app)


def test_sim_tokens_404s_without_env_flag(monkeypatch):
    monkeypatch.delenv("PRISM_DEV_SIM", raising=False)
    client = _client()
    resp = client.post(
        "/api/work/sim-tokens?project=gamify",
        json={"task_id": "T-1", "session_id": "S-1", "out_tokens": 100},
    )
    assert resp.status_code == 404, (
        f"sim-tokens must 404 when PRISM_DEV_SIM is unset; got {resp.status_code}")


def test_sim_tokens_404s_when_flag_is_not_exactly_one(monkeypatch):
    monkeypatch.setenv("PRISM_DEV_SIM", "true")  # truthy-looking, but not "1"
    client = _client()
    resp = client.post(
        "/api/work/sim-tokens?project=gamify",
        json={"task_id": "T-1", "session_id": "S-1", "out_tokens": 100},
    )
    assert resp.status_code == 404, (
        f"sim-tokens must 404 unless PRISM_DEV_SIM is exactly '1'; got {resp.status_code}")


async def _drain_after(body) -> list[dict]:
    """See test_work_bus_publishers.py's _drain_after — the sleep(0) lets
    the cross-thread call_soon_threadsafe callback land before draining."""
    from prism_service.events import bus

    q = bus.subscribe()
    try:
        body()
        await asyncio.sleep(0)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return events
    finally:
        bus.unsubscribe(q)


def test_sim_tokens_publishes_when_flag_is_set(monkeypatch):
    monkeypatch.setenv("PRISM_DEV_SIM", "1")
    client = _client()
    row = {
        "task_id": "T-sim-1", "session_id": "S-sim-1",
        "out_tokens": 250, "dt_s": 1.5, "tok_s": 166.7, "tokens_total": 5000,
    }

    def _post():
        resp = client.post("/api/work/sim-tokens?project=gamify", json=row)
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    events = asyncio.run(_drain_after(_post))
    assert events, "sim-tokens must publish a tokens.turn event when PRISM_DEV_SIM=1"
    evt = events[-1]
    assert evt.get("project") == "gamify", f"got {evt!r}"
    assert evt.get("type") == "tokens.turn", f"got {evt!r}"
    assert evt.get("task_id") == "T-sim-1", f"got {evt!r}"
    assert evt.get("session_id") == "S-sim-1", f"got {evt!r}"
    assert evt.get("out_tokens") == 250, f"got {evt!r}"
