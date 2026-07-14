"""Task-detail Trace tab endpoint (UI redesign, workstream 5).

Drives the REAL tasks router with a FastAPI TestClient over a REAL
scores.db on disk, seeding agent_runs rows the same way production's
telemetry channel does. Pins the user-facing seam end to end:

  * GET /api/tasks/{id}/trace groups the task's agent_runs by session,
    then by SDLC step, tokens on every row;
  * per-session tokens_total and the {tokens, steps, sessions} totals
    sum the seeded rows exactly;
  * steps within a session stay time-ordered (started_at ASC);
  * a task with NO runs returns empty arrays (honest empty state), not
    a 404 — the tab renders an empty trace.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _seed_schema(scores_db: str) -> None:
    """Create scores.db (incl. agent_runs) via Brain's CREATE TABLE block —
    the same path production takes."""
    from prism_service.engines.brain_engine import Brain
    Brain(
        brain_db=str(Path(scores_db).parent / "brain.db"),
        graph_db=str(Path(scores_db).parent / "graph.db"),
        scores_db=scores_db,
    )


def _run(**kw) -> dict:
    base = dict(
        run_id="run-1", workflow_name="implement", task_id="T-trace",
        session_id="S-1", agent_id="agent-a", parent_agent_id=None,
        role="dev", step="implement", model="claude-opus-4-8",
        started_at="2026-05-31T12:00:00+00:00",
        ended_at="2026-05-31T12:01:00+00:00", duration_ms=60000,
        tokens=1000, tool_uses=3, ok=True, gate_state="none",
        verdict_summary="", evidence_ref="",
    )
    base.update(kw)
    return base


def _client(tmp_path, monkeypatch):
    """Mount the REAL tasks router over a REAL scores.db in tmp. get_project
    is patched so the trace route resolves scores_db via ctx._data_dir."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import tasks as tasks_api

    scores_db = str(tmp_path / "scores.db")
    _seed_schema(scores_db)

    class _Ctx:
        _data_dir = tmp_path

    monkeypatch.setattr(tasks_api, "get_project", lambda p: _Ctx())

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app), scores_db


def _seed(scores_db: str, rows: list[dict]) -> None:
    from prism_service.services.agent_runs_data import upsert_agent_run
    for r in rows:
        upsert_agent_run(scores_db, r)


def test_trace_groups_by_session_then_step(tmp_path, monkeypatch):
    client, scores_db = _client(tmp_path, monkeypatch)
    # Two sessions on one task: S-1 has two steps, S-2 one.
    _seed(scores_db, [
        _run(session_id="S-1", agent_id="a1", step="write_failing_tests",
             role="qa", tokens=1400, started_at="2026-05-31T12:00:00+00:00"),
        _run(session_id="S-1", agent_id="a2", step="implement",
             role="dev", tokens=6300, started_at="2026-05-31T12:05:00+00:00"),
        _run(session_id="S-2", agent_id="a3", step="verify_green",
             role="qa", tokens=900, gate_state="passed",
             started_at="2026-05-31T13:00:00+00:00"),
    ])
    got = client.get("/api/tasks/T-trace/trace", params={"project": "prism"})
    assert got.status_code == 200, got.text
    body = got.json()

    # Totals sum every row across both sessions.
    assert body["totals"] == {"tokens": 1400 + 6300 + 900, "steps": 3,
                              "sessions": 2}, body["totals"]

    sessions = {s["session_id"]: s for s in body["sessions"]}
    assert set(sessions) == {"S-1", "S-2"}
    # Per-session tokens_total.
    assert sessions["S-1"]["tokens_total"] == 1400 + 6300
    assert sessions["S-2"]["tokens_total"] == 900
    # Steps time-ordered within S-1.
    s1_steps = [st["step"] for st in sessions["S-1"]["steps"]]
    assert s1_steps == ["write_failing_tests", "implement"], s1_steps
    # Each step row carries role/model/tokens/gate_state/ts.
    first = sessions["S-1"]["steps"][0]
    for f in ("step", "role", "model", "tokens", "gate_state", "ts"):
        assert f in first, f"step row missing {f!r}: {first}"
    assert first["role"] == "qa" and first["tokens"] == 1400
    assert sessions["S-2"]["steps"][0]["gate_state"] == "passed"


def test_trace_empty_for_task_with_no_runs(tmp_path, monkeypatch):
    """A task with no agent_runs returns empty arrays + zeroed totals, not a
    404 — the tab renders an honest empty state."""
    client, _ = _client(tmp_path, monkeypatch)
    got = client.get("/api/tasks/nobody/trace", params={"project": "prism"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["sessions"] == []
    assert body["totals"] == {"tokens": 0, "steps": 0, "sessions": 0}


def test_trace_scoped_to_one_task(tmp_path, monkeypatch):
    """Rows from a different task never leak into this task's trace."""
    client, scores_db = _client(tmp_path, monkeypatch)
    _seed(scores_db, [
        _run(task_id="T-trace", session_id="S-1", agent_id="a1",
             step="implement", tokens=500),
        _run(task_id="OTHER", session_id="S-9", agent_id="z9",
             step="implement", tokens=99999),
    ])
    body = client.get("/api/tasks/T-trace/trace",
                      params={"project": "prism"}).json()
    assert body["totals"]["tokens"] == 500, body
    assert {s["session_id"] for s in body["sessions"]} == {"S-1"}
