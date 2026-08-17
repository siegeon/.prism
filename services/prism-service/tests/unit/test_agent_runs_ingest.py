"""RED — blank-session telemetry must not reach the /live bus (task ea92640f).

The ingest route stores EVERY row (audit spine) but must never publish an
`agent.run` bus event whose session_id is empty/whitespace — that event is
what births ghost session cards on the /live board (graphState.ts agent.run
handler). Harness mirrors test_api_agent_runs.py: real router, real
scores.db in tmp, get_project patched; the events bus is monkeypatched to
capture publishes.
"""

from __future__ import annotations

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


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import agent_runs as agent_runs_api

    data_dir = tmp_path
    scores_db = str(data_dir / "scores.db")
    _seed_schema(scores_db)

    class _Ctx:
        _data_dir = data_dir

    monkeypatch.setattr(agent_runs_api, "get_project", lambda p: _Ctx())

    published = []
    from prism_service.events import bus
    monkeypatch.setattr(bus, "publish", lambda ev: published.append(ev))

    app = FastAPI()
    app.include_router(agent_runs_api.router, prefix="/api/agent-runs")
    return TestClient(app), scores_db, published


def _row(session_id: str) -> dict:
    return dict(
        run_id="run-ghost", workflow_name="implement", task_id="T-ghost",
        session_id=session_id, agent_id="agent-g", parent_agent_id=None,
        role="dev", step="implement_tasks", model="m", ok=True,
    )


def test_blank_session_id_row_stored_but_no_bus_event(tmp_path, monkeypatch):
    """AC-1: '' and whitespace session ids store the row (audit spine)
    but publish NO agent.run event — the ghost-card maker stays off the bus."""
    import sqlite3
    client, scores_db, published = _client(tmp_path, monkeypatch)
    for sid in ("", "   "):
        r = client.post("/api/agent-runs/ingest?project=p", json=_row(sid))
        assert r.status_code == 200 and r.json().get("ok") is True
    rows = sqlite3.connect(scores_db).execute(
        "SELECT COUNT(*) FROM agent_runs WHERE task_id='T-ghost'").fetchone()[0]
    assert rows >= 1, "the audit-spine row must still be stored"
    blank_events = [e for e in published
                    if not str(e.get("session_id") or "").strip()]
    assert blank_events == [], (
        "a blank session_id must never reach the bus: %r" % blank_events)


def test_real_session_id_still_publishes(tmp_path, monkeypatch):
    """Regression guard: a genuine session id keeps today's bus event."""
    client, _scores_db, published = _client(tmp_path, monkeypatch)
    r = client.post("/api/agent-runs/ingest?project=p", json=_row("S-real"))
    assert r.status_code == 200
    assert any(e.get("session_id") == "S-real" and e.get("type") == "agent.run"
               for e in published), "real-session agent.run event must publish"
