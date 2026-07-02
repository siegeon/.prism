"""/api/drive/metrics — PI drive KPIs, baseline vs target (task a14949b9).

FR-8 of the PI-orchestration program (parent 81b23574): a measured
baseline-vs-target surface over the two run ledgers so the DriveEngine
work can PROVE improvement (feeds G0/G3 of the self-improvement
program). Five KPIs, each returned as {measured, baseline, target}:

    task_attributed_drive_rate   pi_runs rows carrying a task_id / window
    first_try_gate_pass_rate     gate rows passed with NO override/blind
    per_step_latency_p50         median duration_ms across the window (ms)
    drive_completion_rate        pi_runs ok rows / window
    tokens_per_drive             attributed tokens / distinct driven tasks

Sources: the pi_runs JSONL manifest (services/pi_run_log.py — THE ledger
for backend pi|local) and the agent_runs telemetry table in scores.db
(services/agent_runs_data.py). Baselines are FROZEN from the live-ledger
receipts of 2026-07-02 (last 200 rows: 1 attributed = 0.005, 159 ok =
0.795, the one attributed drive = 163 tokens over a 7160 ms ≈ 7.2 s
exchange); targets are the program goals from the parent plan_doc.

Self-contained router — registration in api/__init__.py is OWNED by the
C4 seam task (a7d96437). The one-line include:

    api_router.include_router(drive_metrics_router, prefix="/drive",
                              tags=["drive"])
"""

from __future__ import annotations

import statistics

from fastapi import APIRouter, Query

from prism_service.project_context import get_project
from prism_service.services import pi_run_log
from prism_service.services.agent_runs_data import get_agent_runs

router = APIRouter()

# Measurement windows (bounded reads — both ledgers are small).
PI_WINDOW = 200
GATE_ROW_LIMIT = 2000

# Baselines FROZEN from the live pi_runs ledger, 2026-07-02 (see module
# docstring). first_try_gate_pass_rate baseline is 0.0 per the gate-
# theater doctrine: pre-engine drives self-overrode every gate.
BASELINES = {
    "task_attributed_drive_rate": 0.005,   # 1 of 200 rows
    "first_try_gate_pass_rate": 0.0,
    "per_step_latency_p50": 7200.0,        # ms — ~7.2 s/exchange
    "drive_completion_rate": 0.795,        # 159 of 200 rows ok
    "tokens_per_drive": 163.0,             # the one attributed drive
}

# Program targets (parent 81b23574 plan_doc goals).
TARGETS = {
    "task_attributed_drive_rate": 0.95,
    "first_try_gate_pass_rate": 0.90,
    "per_step_latency_p50": 2000.0,        # ms — engine, not prose
    "drive_completion_rate": 0.95,
    "tokens_per_drive": 163.0,             # hold-or-improve ceiling
}

# Verdict markers that disqualify a gate pass from "first try" — the
# same vocabulary get_agent_run_aggregates uses for its override rate.
_OVERRIDE_MARKERS = ("override", "blind")


def _kpi(name: str, measured) -> dict:
    return {"measured": measured, "baseline": BASELINES[name],
            "target": TARGETS[name]}


def compute_kpis(pi_rows: list[dict], agent_rows: list[dict]) -> dict:
    """Pure KPI rollup over ledger rows — no I/O, unit-testable."""
    n = len(pi_rows)
    attributed = [r for r in pi_rows if str(r.get("task_id") or "")]
    attr_rate = (len(attributed) / n) if n else 0.0
    ok_rate = (sum(1 for r in pi_rows if r.get("ok")) / n) if n else 0.0
    durations = [float(r.get("duration_ms") or 0) for r in pi_rows]
    p50 = float(statistics.median(durations)) if durations else 0.0
    drives = {str(r.get("task_id")) for r in attributed}
    attr_tokens = sum(int(r.get("tokens") or 0) for r in attributed)
    tokens_per_drive = (attr_tokens / len(drives)) if drives else None

    # Gate KPI: only *_gate step rows count; "first try" excludes any
    # pass whose verdict carries an override/blind recovery marker.
    gates = [r for r in agent_rows
             if str(r.get("step") or "").endswith("_gate")]
    clean = [
        r for r in gates
        if str(r.get("gate_state") or "") == "passed"
        and not any(m in str(r.get("verdict_summary") or "").lower()
                    for m in _OVERRIDE_MARKERS)
    ]
    gate_rate = (len(clean) / len(gates)) if gates else 0.0

    return {
        "task_attributed_drive_rate": _kpi(
            "task_attributed_drive_rate", attr_rate),
        "first_try_gate_pass_rate": _kpi(
            "first_try_gate_pass_rate", gate_rate),
        "per_step_latency_p50": _kpi("per_step_latency_p50", p50),
        "drive_completion_rate": _kpi("drive_completion_rate", ok_rate),
        "tokens_per_drive": _kpi("tokens_per_drive", tokens_per_drive),
        "window": {"pi_runs": n, "gate_rows": len(gates)},
    }


@router.get("/metrics")
def drive_metrics(project: str = Query("default")) -> dict:
    """Baseline vs target KPIs for the PI drive program.

    pi_runs is a DATA_DIR-global ledger (program-wide window, no project
    filter); agent_runs lives in the project's scores.db — resolved the
    same way api/agent_runs.py does. Both reads degrade to empty rows so
    a fresh install still answers with the full 5-KPI shape.
    """
    pi_rows = pi_run_log.list_recent(limit=PI_WINDOW)
    try:
        scores_db = str(get_project(project)._data_dir / "scores.db")
        agent_rows = get_agent_runs(scores_db, limit=GATE_ROW_LIMIT)
    except Exception:
        agent_rows = []
    return compute_kpis(pi_rows, agent_rows)
