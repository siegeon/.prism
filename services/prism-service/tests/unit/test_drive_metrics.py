"""RED scaffold — drive metrics KPIs, baseline vs target (task a14949b9).

Pins GET /api/drive/metrics (parent 81b23574 FR-8/AC-8): the 5 program
KPIs (task_attributed_drive_rate, first_try_gate_pass_rate,
per_step_latency_p50, drive_completion_rate, tokens_per_drive), each
returned as {measured, baseline, target}, computed from the REAL
ledgers — the pi_runs JSONL manifest (services/pi_run_log.py) and the
agent_runs table in scores.db (services/agent_runs_data.py).

ALL FAIL today: prism_service/api/drive_metrics.py does not exist and
the live daemon 404s on /api/drive/metrics (receipt: HTTP 404 on
http://127.0.0.1:9999/api/drive/metrics, 2026-07-02). Baseline receipts
from the live ledger (last 200 rows): 1 task-attributed (0.005),
159 ok (0.795), the one attributed drive = 163 tokens @ 7160 ms.

Router is mounted DIRECTLY on a test app under /api/drive — route
registration in api/__init__.py is owned by C4 (a7d96437), not here.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

KPI_KEYS = (
    "task_attributed_drive_rate",
    "first_try_gate_pass_rate",
    "per_step_latency_p50",
    "drive_completion_rate",
    "tokens_per_drive",
)


def _pi(**kw) -> dict:
    """One pi_runs manifest row (only the fields the KPIs read matter)."""
    base = dict(
        run_id="r-000000000000", ts=1.0, ts_end=2.0, duration_ms=100.0,
        backend="pi", model="qwen3:1.7b", purpose="panel-drive",
        project="prism", prompt_chars=10, tools_used=[], turns=1,
        tokens=0, input_tokens=0, output_tokens=0, task_id="",
        ok=True, error="",
    )
    base.update(kw)
    return base


def _gate(**kw) -> dict:
    """One agent_runs row (gate telemetry shape)."""
    base = dict(
        run_id="run-1", workflow_name="implement", task_id="T-1",
        session_id="S-1", agent_id="agent-a", parent_agent_id=None,
        role="driver", step="story_gate", model="m",
        started_at="2026-07-02T12:00:00+00:00",
        ended_at="2026-07-02T12:01:00+00:00", duration_ms=1000,
        tokens=10, tool_uses=1, ok=1, gate_state="passed",
        verdict_summary="rubric pass", evidence_ref="",
    )
    base.update(kw)
    return base


_AGENT_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT, workflow_name TEXT, task_id TEXT, session_id TEXT,
    agent_id TEXT, parent_agent_id TEXT, role TEXT, step TEXT,
    model TEXT, started_at TEXT, ended_at TEXT, duration_ms INTEGER,
    tokens INTEGER, tool_uses INTEGER, ok INTEGER, gate_state TEXT,
    verdict_summary TEXT, evidence_ref TEXT,
    recorded_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, agent_id, step)
)
"""


def _seed_scores_db(data_dir: Path, gate_rows: list[dict]) -> None:
    from prism_service.services.agent_runs_data import upsert_agent_run
    scores_db = str(data_dir / "scores.db")
    conn = sqlite3.connect(scores_db)
    try:
        conn.execute(_AGENT_RUNS_DDL)
        conn.commit()
    finally:
        conn.close()
    for row in gate_rows:
        upsert_agent_run(scores_db, row)


def _client(tmp_path, monkeypatch, pi_rows=(), gate_rows=()):
    """Mount the REAL drive_metrics router over REAL tmp ledgers:
    pi_run_log is pointed at a tmp manifest.jsonl (the module reads its
    _MANIFEST global) and get_project resolves scores.db to tmp — the
    exact seams api/pi_runs.py and api/agent_runs.py use in prod."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.services import pi_run_log
    from prism_service.api import drive_metrics as drive_metrics_api

    runs_dir = tmp_path / "pi_runs"
    manifest = runs_dir / "manifest.jsonl"
    monkeypatch.setattr(pi_run_log, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(pi_run_log, "_MANIFEST", manifest)
    if pi_rows:
        runs_dir.mkdir(parents=True, exist_ok=True)
        with manifest.open("w", encoding="utf-8") as fh:
            for r in pi_rows:
                fh.write(json.dumps(r) + "\n")

    _seed_scores_db(tmp_path, list(gate_rows))

    class _Ctx:
        _data_dir = tmp_path

    monkeypatch.setattr(drive_metrics_api, "get_project", lambda p: _Ctx())

    app = FastAPI()
    app.include_router(drive_metrics_api.router, prefix="/api/drive")
    return TestClient(app)


# ----------------------------------------------------------------------
# AC-1: all 5 KPIs, each {measured, baseline, target}; the plan-frozen
# baselines (0.5% attribution, 7.2s exchange, 79.5% ok) are visible.
# ----------------------------------------------------------------------


def test_metrics_returns_all_5_kpis_with_baseline_and_target(
        tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch,
                     pi_rows=[_pi()], gate_rows=[_gate()])
    resp = client.get("/api/drive/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in KPI_KEYS:
        assert key in body, f"KPI {key!r} missing: {list(body)}"
        kpi = body[key]
        for field in ("measured", "baseline", "target"):
            assert field in kpi, f"{key} missing {field!r}: {kpi}"
    # Plan-frozen baselines (live-ledger receipts, 2026-07-02).
    assert body["task_attributed_drive_rate"]["baseline"] == 0.005
    assert body["drive_completion_rate"]["baseline"] == 0.795
    assert body["per_step_latency_p50"]["baseline"] == 7200.0
    assert body["tokens_per_drive"]["baseline"] == 163.0
    assert body["first_try_gate_pass_rate"]["baseline"] == 0.0
    # Targets are the program goals — improvement over baseline.
    assert body["task_attributed_drive_rate"]["target"] > 0.9
    assert body["first_try_gate_pass_rate"]["target"] >= 0.9
    assert body["drive_completion_rate"]["target"] > 0.9
    assert body["per_step_latency_p50"]["target"] < 7200.0
    # Window receipt: how many ledger rows fed the measurement.
    assert "window" in body, list(body)
    assert body["window"]["pi_runs"] == 1


# ----------------------------------------------------------------------
# AC-2: measured values DERIVE from the pi_runs ledger rows.
# ----------------------------------------------------------------------


def test_measured_values_derive_from_pi_ledger(tmp_path, monkeypatch):
    rows = [
        _pi(run_id="a1", task_id="2a5e72a7-attr", tokens=163,
            duration_ms=7000.0, ok=True),
        _pi(run_id="a2", duration_ms=100.0, ok=True),
        _pi(run_id="a3", duration_ms=200.0, ok=True),
        _pi(run_id="a4", duration_ms=300.0, ok=False),
    ]
    client = _client(tmp_path, monkeypatch, pi_rows=rows)
    body = client.get("/api/drive/metrics").json()
    # 1 of 4 rows task-attributed.
    assert body["task_attributed_drive_rate"]["measured"] == 0.25
    # 3 of 4 rows ok.
    assert body["drive_completion_rate"]["measured"] == 0.75
    # p50 = median(100, 200, 300, 7000) = 250.0 ms.
    assert body["per_step_latency_p50"]["measured"] == 250.0
    # 163 tokens over 1 distinct attributed drive.
    assert body["tokens_per_drive"]["measured"] == 163.0
    assert body["window"]["pi_runs"] == 4


def test_tokens_per_drive_averages_across_distinct_drives(
        tmp_path, monkeypatch):
    rows = [
        _pi(run_id="b1", task_id="T-A", tokens=100),
        _pi(run_id="b2", task_id="T-A", tokens=50),
        _pi(run_id="b3", task_id="T-B", tokens=150),
        _pi(run_id="b4", tokens=999),  # unattributed: excluded
    ]
    client = _client(tmp_path, monkeypatch, pi_rows=rows)
    body = client.get("/api/drive/metrics").json()
    # (100 + 50 + 150) tokens over 2 distinct drives = 150.0.
    assert body["tokens_per_drive"]["measured"] == 150.0


# ----------------------------------------------------------------------
# AC-3: first_try_gate_pass_rate = gate-step rows passed WITHOUT an
# override/blind verdict, over ALL gate-step rows; non-gate rows excluded.
# ----------------------------------------------------------------------


def test_first_try_gate_pass_rate_excludes_overrides(tmp_path, monkeypatch):
    gates = [
        _gate(run_id="g1", agent_id="a1", step="story_gate",
              gate_state="passed", verdict_summary="rubric pass"),
        _gate(run_id="g2", agent_id="a2", step="red_gate",
              gate_state="passed",
              verdict_summary="manual-override: blind verifier recovery"),
        _gate(run_id="g3", agent_id="a3", step="green_gate",
              gate_state="failed", verdict_summary="suite red"),
        # Non-gate step: must not enter numerator or denominator.
        _gate(run_id="g4", agent_id="a4", step="implement_tasks",
              gate_state="none", verdict_summary="ok"),
    ]
    client = _client(tmp_path, monkeypatch, gate_rows=gates)
    body = client.get("/api/drive/metrics").json()
    measured = body["first_try_gate_pass_rate"]["measured"]
    assert abs(measured - (1 / 3)) < 1e-9, (
        f"expected 1 clean pass of 3 gate rows, got {measured}"
    )
    assert body["window"]["gate_rows"] == 3


# ----------------------------------------------------------------------
# AC-4: empty ledgers -> 200 with zeros/null, never a 500.
# ----------------------------------------------------------------------


def test_empty_ledgers_return_full_shape_not_500(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)  # no manifest, empty db
    resp = client.get("/api/drive/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in KPI_KEYS:
        assert key in body
    assert body["task_attributed_drive_rate"]["measured"] == 0.0
    assert body["drive_completion_rate"]["measured"] == 0.0
    assert body["first_try_gate_pass_rate"]["measured"] == 0.0
    assert body["per_step_latency_p50"]["measured"] == 0.0
    # No attributed drive yet: tokens_per_drive is unknowable, not 0.
    assert body["tokens_per_drive"]["measured"] is None
    assert body["window"] == {"pi_runs": 0, "gate_rows": 0}
