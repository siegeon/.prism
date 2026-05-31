"""RED scaffold — agent-run telemetry spine (task f4498190).

Drives the REAL agent_runs router with a FastAPI TestClient over a REAL
scores.db on disk. This pins the USER-FACING seam end-to-end, not just a
service method:

  * the route is reachable through the REGISTERED api_router at
    /api/agent-runs (mounted via api/__init__.py — not just defined);
  * POST /api/agent-runs/ingest persists one telemetry row to scores.db;
  * a SEPARATE GET /api/agent-runs reads it back (proves on-disk
    persistence, not one in-memory instance);
  * writes are idempotent on (run_id, agent_id, step) — re-POSTing the
    same triple UPDATES rather than duplicates;
  * GET honors filters: task_id, session_id, workflow_name, role, step.

ALL FAIL today: there is no agent_runs table, no api/agent_runs.py
module, and no include_router line in api/__init__.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _seed_schema(scores_db: str) -> None:
    """Create the scores.db schema (incl. the new agent_runs table) the
    same way production does — via Brain's CREATE TABLE IF NOT EXISTS
    block in brain_engine.py."""
    from prism_service.engines.brain_engine import Brain
    Brain(
        brain_db=str(Path(scores_db).parent / "brain.db"),
        graph_db=str(Path(scores_db).parent / "graph.db"),
        scores_db=scores_db,
    )


def _row(**kw) -> dict:
    base = dict(
        run_id="run-1",
        workflow_name="implement",
        task_id="T-1",
        session_id="S-1",
        agent_id="agent-aaa",
        parent_agent_id=None,
        role="qa",
        step="write_failing_tests",
        model="claude-opus-4-8",
        started_at="2026-05-31T12:00:00+00:00",
        ended_at="2026-05-31T12:01:00+00:00",
        duration_ms=60000,
        tokens=12345,
        tool_uses=7,
        ok=True,
        gate_state="none",
        verdict_summary="red trace captured",
        evidence_ref="sha:abc1234",
    )
    base.update(kw)
    return base


def _client(tmp_path, monkeypatch):
    """Mount the REAL agent_runs router over a REAL scores.db in tmp.
    get_project(...) is patched so the router resolves scores_db via
    ctx._data_dir / 'scores.db' — mirroring api/learning.py."""
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
    return TestClient(app), scores_db


# ----------------------------------------------------------------------
# Route reachable through the REGISTERED api_router (dispatcher, not just
# a module-local include). This is the false-green guard: a defined
# router that is never mounted into api_router would 404 in production.
# ----------------------------------------------------------------------


def test_route_registered_on_api_router():
    from prism_service.api import api_router
    paths = {r.path for r in api_router.routes}
    assert "/api/agent-runs" in paths, (
        f"GET /api/agent-runs not mounted on api_router: {sorted(paths)}"
    )
    assert "/api/agent-runs/ingest" in paths, (
        f"POST /api/agent-runs/ingest not mounted on api_router: {sorted(paths)}"
    )


# ----------------------------------------------------------------------
# POST -> (separate) GET round-trip proves ON-DISK persistence.
# ----------------------------------------------------------------------


def test_ingest_then_get_roundtrip_across_separate_calls(tmp_path, monkeypatch):
    client, scores_db = _client(tmp_path, monkeypatch)
    post = client.post(
        "/api/agent-runs/ingest",
        params={"project": "prism"},
        json=_row(),
    )
    assert post.status_code == 200, post.text

    # SEPARATE GET call — must read the row back off scores.db.
    got = client.get("/api/agent-runs", params={"project": "prism"})
    assert got.status_code == 200, got.text
    rows = got.json()["rows"]
    assert len(rows) == 1, f"expected exactly one persisted row, got {rows}"
    r = rows[0]
    for field in (
        "run_id", "workflow_name", "task_id", "session_id", "agent_id",
        "parent_agent_id", "role", "step", "model", "started_at",
        "ended_at", "duration_ms", "tokens", "tool_uses", "ok",
        "gate_state", "verdict_summary", "evidence_ref",
    ):
        assert field in r, f"persisted row missing column {field!r}: {r}"
    assert r["run_id"] == "run-1"
    assert r["role"] == "qa"
    assert r["step"] == "write_failing_tests"
    assert int(r["tokens"]) == 12345


def test_ingest_persists_to_disk_not_memory(tmp_path, monkeypatch):
    """A fresh TestClient over the SAME scores_db still sees the row —
    proves the write landed on disk, not in a per-request instance."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import agent_runs as agent_runs_api

    client, scores_db = _client(tmp_path, monkeypatch)
    client.post("/api/agent-runs/ingest", params={"project": "prism"},
                json=_row(run_id="durable", agent_id="ag-d"))

    # Brand-new app/client pointed at the same on-disk scores.db.
    class _Ctx:
        _data_dir = Path(scores_db).parent
    monkeypatch.setattr(agent_runs_api, "get_project", lambda p: _Ctx())
    app2 = FastAPI()
    app2.include_router(agent_runs_api.router, prefix="/api/agent-runs")
    client2 = TestClient(app2)

    rows = client2.get("/api/agent-runs", params={"project": "prism"}).json()["rows"]
    assert any(r["run_id"] == "durable" for r in rows), (
        "row did not survive into a separate client -> not on disk"
    )


# ----------------------------------------------------------------------
# Idempotency on (run_id, agent_id, step): re-POST updates, not dupes.
# ----------------------------------------------------------------------


def test_ingest_idempotent_on_run_agent_step(tmp_path, monkeypatch):
    client, scores_db = _client(tmp_path, monkeypatch)
    client.post("/api/agent-runs/ingest", params={"project": "prism"},
                json=_row(verdict_summary="first", tokens=100))
    # Same (run_id, agent_id, step) — different payload values.
    client.post("/api/agent-runs/ingest", params={"project": "prism"},
                json=_row(verdict_summary="second", tokens=999))

    rows = client.get("/api/agent-runs", params={"project": "prism"}).json()["rows"]
    same = [r for r in rows
            if r["run_id"] == "run-1" and r["agent_id"] == "agent-aaa"
            and r["step"] == "write_failing_tests"]
    assert len(same) == 1, f"re-POST must UPDATE, not duplicate: {same}"
    assert same[0]["verdict_summary"] == "second", "row was not updated on re-POST"
    assert int(same[0]["tokens"]) == 999


def test_distinct_step_is_a_separate_row(tmp_path, monkeypatch):
    """Same run+agent but a DIFFERENT step is a distinct telemetry row —
    the agent-path of a run is preserved, not collapsed."""
    client, _ = _client(tmp_path, monkeypatch)
    client.post("/api/agent-runs/ingest", params={"project": "prism"},
                json=_row(step="write_failing_tests"))
    client.post("/api/agent-runs/ingest", params={"project": "prism"},
                json=_row(step="implement_tasks"))
    rows = client.get("/api/agent-runs", params={"project": "prism"}).json()["rows"]
    steps = sorted(r["step"] for r in rows if r["run_id"] == "run-1")
    assert steps == ["implement_tasks", "write_failing_tests"], steps


# ----------------------------------------------------------------------
# Filters: task_id, session_id, workflow_name, role, step.
# ----------------------------------------------------------------------


def _seed_mixed(client):
    client.post("/api/agent-runs/ingest", params={"project": "prism"}, json=_row(
        run_id="r-a", agent_id="a1", task_id="T-A", session_id="S-A",
        workflow_name="implement", role="qa", step="write_failing_tests"))
    client.post("/api/agent-runs/ingest", params={"project": "prism"}, json=_row(
        run_id="r-b", agent_id="a2", task_id="T-B", session_id="S-B",
        workflow_name="prototype", role="dev", step="implement_tasks"))


@pytest.mark.parametrize("param,value,expect_run", [
    ("task_id", "T-A", "r-a"),
    ("session_id", "S-B", "r-b"),
    ("workflow_name", "prototype", "r-b"),
    ("role", "qa", "r-a"),
    ("step", "implement_tasks", "r-b"),
])
def test_get_honors_filter(tmp_path, monkeypatch, param, value, expect_run):
    client, _ = _client(tmp_path, monkeypatch)
    _seed_mixed(client)
    got = client.get("/api/agent-runs",
                     params={"project": "prism", param: value})
    assert got.status_code == 200, got.text
    rows = got.json()["rows"]
    assert rows, f"filter {param}={value} returned nothing"
    assert all(str(r[param]) == value for r in rows), (
        f"filter {param}={value} leaked non-matching rows: {rows}"
    )
    assert {r["run_id"] for r in rows} == {expect_run}, rows


# ----------------------------------------------------------------------
# Per-task agent cost + agent-path rollup into a learning surface.
# ----------------------------------------------------------------------


def test_per_task_agent_rollup_cost_and_path(tmp_path, monkeypatch):
    """The learning surface rolls agent_runs up per task: total token
    cost + the ordered agent-path (role/step sequence). This is the
    self-learn signal the Tier-3 loop reads."""
    from prism_service.services.agent_runs_data import get_task_agent_rollup

    client, scores_db = _client(tmp_path, monkeypatch)
    # Two steps of one run on task T-roll, in order.
    client.post("/api/agent-runs/ingest", params={"project": "prism"}, json=_row(
        run_id="rr", agent_id="a1", task_id="T-roll", role="qa",
        step="write_failing_tests", tokens=1000,
        started_at="2026-05-31T12:00:00+00:00"))
    client.post("/api/agent-runs/ingest", params={"project": "prism"}, json=_row(
        run_id="rr", agent_id="a2", task_id="T-roll", role="dev",
        step="implement_tasks", tokens=2500,
        started_at="2026-05-31T12:05:00+00:00"))

    roll = get_task_agent_rollup(scores_db, task_id="T-roll")
    assert roll, "rollup returned nothing for a task with agent_runs"
    assert int(roll["total_tokens"]) == 3500, roll
    # Ordered agent-path: role/step in chronological order.
    path = roll["agent_path"]
    assert path[0]["step"] == "write_failing_tests"
    assert path[1]["step"] == "implement_tasks"
    assert path[0]["role"] == "qa" and path[1]["role"] == "dev"


def test_learning_overview_surfaces_agent_runs(tmp_path, monkeypatch):
    """The /api/learning overview must expose agent-run aggregates so the
    /learning-page panel has data — not a separate orphan endpoint."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import learning as learning_api
    from prism_service.api import agent_runs as agent_runs_api

    data_dir = tmp_path
    scores_db = str(data_dir / "scores.db")
    _seed_schema(scores_db)

    class _Ctx:
        _data_dir = data_dir
    monkeypatch.setattr(agent_runs_api, "get_project", lambda p: _Ctx())
    monkeypatch.setattr(learning_api, "get_project", lambda p: _Ctx())

    app = FastAPI()
    app.include_router(agent_runs_api.router, prefix="/api/agent-runs")
    app.include_router(learning_api.router, prefix="/api/learning")
    client = TestClient(app)

    client.post("/api/agent-runs/ingest", params={"project": "prism"},
                json=_row(task_id="T-learn", tokens=4242))

    body = client.get("/api/learning", params={"project": "prism"}).json()
    assert "agent_runs" in body, (
        f"/api/learning must surface agent_runs aggregates: keys={list(body)}"
    )
