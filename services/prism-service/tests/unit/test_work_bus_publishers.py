"""gamify walking skeleton -- bus publishers on the two ingest doors.

POST /api/drive-heartbeat/beat and POST /api/agent-runs/ingest are the
first two of three NEW bus publishers backing /sse/work + the /live graph
(drive.heartbeat, agent.run; the third, tokens.turn, is a poller covered
by test_work_ticker_publishes_tokens_turn.py). Drives the REAL routers
with a FastAPI TestClient over a REAL on-disk scores.db, subscribing to
the REAL prism_service.events.bus -- mirrors test_api_agent_runs.py's
_client() helper and test_task_page_payload_scope.py's bus-drain pattern.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _seed_schema(scores_db: str) -> None:
    from prism_service.engines.brain_engine import Brain
    Brain(
        brain_db=str(Path(scores_db).parent / "brain.db"),
        graph_db=str(Path(scores_db).parent / "graph.db"),
        scores_db=scores_db,
    )


async def _drain_after(body) -> list[dict]:
    """Subscribe to the REAL bus, run `body`, return whatever landed.

    `body` runs synchronously (TestClient's own portal thread does the
    actual publish() call), so the follow-up `asyncio.sleep(0)` is what
    lets the cross-thread `loop.call_soon_threadsafe` callback actually
    land on THIS loop before we drain the queue (mirrors
    test_task_page_payload_scope.py's _drain_after)."""
    from prism_service.events import bus

    q = bus.subscribe()
    try:
        body()
        await asyncio.sleep(0)  # let call_soon_threadsafe callbacks land
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return events
    finally:
        bus.unsubscribe(q)


# ---------------------------------------------------------------------------
# drive.heartbeat
# ---------------------------------------------------------------------------

def _heartbeat_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import drive_heartbeat as hb_api

    data_dir = tmp_path

    class _Ctx:
        _data_dir = data_dir

    monkeypatch.setattr(hb_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(hb_api.router, prefix="/api/drive-heartbeat")
    return TestClient(app)


def test_heartbeat_beat_publishes_drive_heartbeat_event(tmp_path, monkeypatch):
    client = _heartbeat_client(tmp_path, monkeypatch)
    row = {
        "task_id": "T-hb-1",
        "step": "write_failing_tests",
        "elapsed_s": 12.5,
        "last_tool": "Bash",
        "work_units": 3,
    }

    def _post():
        resp = client.post("/api/drive-heartbeat/beat?project=gamify", json=row)
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    events = asyncio.run(_drain_after(_post))
    assert events, (
        "POST /api/drive-heartbeat/beat must publish a drive.heartbeat "
        "event onto the bus so /sse/work has something to forward -- "
        "none landed")
    evt = events[-1]
    assert evt.get("project") == "gamify", f"got {evt!r}"
    assert evt.get("type") == "drive.heartbeat", f"got {evt!r}"
    assert evt.get("task_id") == "T-hb-1", f"got {evt!r}"
    assert evt.get("step") == "write_failing_tests", f"got {evt!r}"
    assert evt.get("last_tool") == "Bash", f"got {evt!r}"
    assert evt.get("work_units") == 3, f"got {evt!r}"
    assert "ts" in evt, f"event must carry a timestamp; got {evt!r}"


def test_heartbeat_refusal_does_not_publish(tmp_path, monkeypatch):
    client = _heartbeat_client(tmp_path, monkeypatch)
    # Missing every required field -> BEAT_REFUSAL, ok:false.
    bad_row = {"task_id": "T-hb-2"}

    def _post():
        resp = client.post("/api/drive-heartbeat/beat?project=gamify", json=bad_row)
        assert resp.status_code == 200
        assert resp.json().get("ok") is False

    events = asyncio.run(_drain_after(_post))
    assert not events, (
        f"a refused (ok:false) heartbeat must never publish; got {events!r}")


# ---------------------------------------------------------------------------
# agent.run
# ---------------------------------------------------------------------------

def _agent_runs_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import agent_runs as agent_runs_api

    data_dir = tmp_path
    scores_db = str(data_dir / "scores.db")
    _seed_schema(scores_db)

    class _Ctx:
        _data_dir = data_dir

    monkeypatch.setattr(agent_runs_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(agent_runs_api.router, prefix="/api/agent-runs")
    return TestClient(app)


def test_agent_runs_ingest_publishes_agent_run_event(tmp_path, monkeypatch):
    client = _agent_runs_client(tmp_path, monkeypatch)
    row = {
        "run_id": "run-work-1",
        "workflow_name": "implement",
        "task_id": "T-ar-1",
        "session_id": "S-ar-1",
        "agent_id": "agent-bbb",
        "parent_agent_id": None,
        "role": "dev",
        "step": "implement_tasks",
        "model": "claude-sonnet-5",
        "gate_state": "none",
        "ok": True,
    }

    def _post():
        resp = client.post("/api/agent-runs/ingest?project=gamify", json=row)
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    events = asyncio.run(_drain_after(_post))
    assert events, (
        "POST /api/agent-runs/ingest must publish an agent.run event onto "
        "the bus so /sse/work has something to forward -- none landed")
    evt = events[-1]
    assert evt.get("project") == "gamify", f"got {evt!r}"
    assert evt.get("type") == "agent.run", f"got {evt!r}"
    assert evt.get("task_id") == "T-ar-1", f"got {evt!r}"
    assert evt.get("session_id") == "S-ar-1", f"got {evt!r}"
    assert evt.get("agent_id") == "agent-bbb", f"got {evt!r}"
    assert evt.get("step") == "implement_tasks", f"got {evt!r}"
    assert "ts" in evt, f"event must carry a timestamp; got {evt!r}"


def test_agent_runs_ingest_refusal_does_not_publish(tmp_path, monkeypatch):
    client = _agent_runs_client(tmp_path, monkeypatch)
    # A producer claiming a passing gate_state is refused by name.
    row = {
        "run_id": "run-work-2", "task_id": "T-ar-2", "session_id": "S-ar-2",
        "agent_id": "agent-ccc", "step": "green_gate", "gate_state": "passed",
    }

    def _post():
        resp = client.post("/api/agent-runs/ingest?project=gamify", json=row)
        assert resp.status_code == 200
        assert resp.json().get("ok") is False

    events = asyncio.run(_drain_after(_post))
    assert not events, (
        f"a refused producer gate-verdict row must never publish; got {events!r}")
