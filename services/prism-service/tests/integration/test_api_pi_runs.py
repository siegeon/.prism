"""RED scaffold — GET /api/pi-runs backs the /internal-agent audit view.

Exercises the REAL FastAPI app (route reachable through the registered
api_router — mounted via api/__init__.py, not just defined): the list
endpoint returns ledger rows newest-first honoring limit + project
passthrough, and the detail endpoint 404s on unknown run ids.

ALL FAIL today: api/pi_runs.py does not exist and nothing is mounted
at /api/pi-runs.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient over the real app with the pi ledger repointed at
    tmp_path (module-level path patch works regardless of how early
    prism_service.config resolved DATA_DIR in a full-suite run)."""
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import prism_service.services.pi_run_log as prl
    runs_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(prl, "_MANIFEST", runs_dir / "manifest.jsonl")

    from fastapi.testclient import TestClient
    from prism_service.main import app
    return TestClient(app)


def _seed(project: str, model: str) -> str:
    import prism_service.services.pi_run_log as prl
    return prl.record_run(
        backend="pi", model=model, purpose="reflect", project=project,
        prompt_chars=10, tools_used=[{"name": "brain_search", "ms": 5.0,
                                      "ok": True}],
        duration_ms=100.0, tokens=9, turns=1, ok=True, error="",
    )


def test_list_newest_first_with_limit_and_project(client):
    a1 = _seed("alpha", "m-1")
    b1 = _seed("beta", "m-2")
    a2 = _seed("alpha", "m-3")

    r = client.get("/api/pi-runs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    assert [x["run_id"] for x in body["runs"]] == [a2, b1, a1]

    r = client.get("/api/pi-runs", params={"limit": 1})
    assert [x["run_id"] for x in r.json()["runs"]] == [a2]

    r = client.get("/api/pi-runs", params={"project": "alpha"})
    assert [x["run_id"] for x in r.json()["runs"]] == [a2, a1]


def test_row_shape_carries_audit_fields(client):
    _seed("alpha", "qwen3:0.6b")
    r = client.get("/api/pi-runs")
    row = r.json()["runs"][0]
    for field in ("run_id", "ts", "backend", "model", "purpose", "project",
                  "prompt_chars", "tools_used", "duration_ms", "tokens",
                  "ok", "error"):
        assert field in row, f"missing audit field {field!r}"


def test_get_single_run_and_404(client):
    rid = _seed("alpha", "m-1")
    r = client.get(f"/api/pi-runs/{rid}")
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == rid

    r = client.get("/api/pi-runs/ffffffffffff")
    assert r.status_code == 404
