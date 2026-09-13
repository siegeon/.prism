"""GET /api/work/graph carries a `timing` block (owner brief 2026-09-13,
"like 200ms, fan out sub agents") so a slow live poll can be diagnosed
from the response itself. Pins the exact phase/count keys and the
X-Prism-Timing response header, over the SAME seeded fixture as
test_api_work_graph.py's minimal root+child graph."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic — timing probe")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    child = task_svc.create(title="Child slice — timing probe", parent_id=root.id)
    task_svc.update(child.id, status="in_progress")

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc
    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    return TestClient(app)


_EXPECTED_PHASE_KEYS = {
    "tasks_query_ms", "edges_ms", "heartbeats_ms", "spend_ms",
    "token_events_ms", "sessions_ms", "serialize_ms", "total_ms",
}
_EXPECTED_COUNT_KEYS = {
    "nodes", "sessions_seen", "transcript_calls", "bounded_timeouts",
    "sqlite_connections_opened",
}


def test_graph_response_carries_timing_block_and_header(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = client.get("/api/work/graph?project=timing-probe")
    assert resp.status_code == 200
    body = resp.json()

    assert "timing" in body, f"no timing block; got keys {sorted(body.keys())}"
    timing = body["timing"]

    missing_phases = _EXPECTED_PHASE_KEYS - set(timing.keys())
    assert not missing_phases, f"timing missing phase keys: {missing_phases}"
    missing_counts = _EXPECTED_COUNT_KEYS - set(timing.keys())
    assert not missing_counts, f"timing missing count keys: {missing_counts}"

    # Every phase ms is a non-negative number, and the phases (excluding
    # total, which is the whole-request wall clock they're each a slice
    # of) never individually exceed the reported total by more than a
    # hair of floating-point/measurement slop.
    for key in _EXPECTED_PHASE_KEYS:
        assert isinstance(timing[key], (int, float)), f"{key} not numeric: {timing[key]!r}"
        assert timing[key] >= 0, f"{key} negative: {timing[key]!r}"
    non_total = sum(timing[k] for k in _EXPECTED_PHASE_KEYS if k != "total_ms")
    assert non_total <= timing["total_ms"] + 5.0, (
        f"phases sum to {non_total}ms, exceeding total {timing['total_ms']}ms "
        "by more than measurement slop -- a phase is double-counted")

    assert timing["nodes"] == len(body["nodes"])

    # The header carries the same total the JSON body reports, so a
    # `curl -D-` one-liner never needs to parse JSON to see it.
    header = resp.headers.get("x-prism-timing", "")
    assert header == f"total_ms={timing['total_ms']}", (
        f"X-Prism-Timing header {header!r} doesn't match body total_ms "
        f"{timing['total_ms']!r}")
