"""RED — POST /api/agent/run records a task-attributed PI-panel row (fc08da8d).

The browser PI agent runs inference locally; its per-exchange usage never
reaches a Claude transcript. A new endpoint — POST /api/agent/run — takes the
exchange USAGE (task_id, model, input/output tokens, ms, tools_used) and records
one pi_runs row with backend='panel', purpose='panel-drive', task_id set, so the
conductor tile burn + task detail can attribute the PI agent's work.

Distinct from POST /api/agent/tool (which DISPATCHES an MCP tool): this endpoint
records usage, it does not call a tool.

FAIL today: the /api/agent/run route does not exist (405/404).
"""

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import prism_service.services.pi_run_log as prl
    runs_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(prl, "_MANIFEST", runs_dir / "manifest.jsonl")

    from fastapi.testclient import TestClient
    from prism_service.main import app

    c = TestClient(app)
    c.post("/api/projects", json={"name": "runtel"})
    return c


def test_run_endpoint_records_task_attributed_row(client):
    import prism_service.services.pi_run_log as prl

    r = client.post(
        "/api/agent/run",
        params={"project": "runtel"},
        json={
            "task_id": "fc08da8d-drive",
            "model": "qwen3:0.6b",
            "input_tokens": 30,
            "output_tokens": 45,
            "ms": 1234,
            "tools_used": [{"name": "conductor_advance", "ms": 12.0, "ok": True}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    run_id = body.get("run_id")
    assert run_id

    rows = prl.list_recent(limit=10, project="runtel", task_id="fc08da8d-drive")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["backend"] == "panel"
    assert row["purpose"] == "panel-drive"
    assert row["task_id"] == "fc08da8d-drive"
    assert row["model"] == "qwen3:0.6b"
    assert row["output_tokens"] == 45
    assert row["input_tokens"] == 30
    assert row["tools_used"] == [{"name": "conductor_advance", "ms": 12.0, "ok": True}]


def test_run_endpoint_records_adhoc_when_no_task(client):
    """No detected task -> still recorded (empty task_id), never a 500."""
    import prism_service.services.pi_run_log as prl

    r = client.post(
        "/api/agent/run",
        params={"project": "runtel"},
        json={"model": "m", "output_tokens": 5},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    rows = prl.list_recent(limit=10, project="runtel")
    assert any(row["task_id"] == "" and row["backend"] == "panel" for row in rows)
