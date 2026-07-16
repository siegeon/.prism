"""Green-gate machine adjudicator sweep — the conductor's own gate seat.

Sweeps every project's tasks parked PENDING at green_gate on a cadence and
lets ConductorService.adjudicate_green_gate decide each one: exercise the
oracle when it is machine-runnable and unevidenced for the current tree,
approve on a FRESH PASSING EvidenceReceipt as ``conductor-adjudicator``,
and leave everything else (manual-evidence oracles, epics, failed gates,
tried-and-failing evidence) for a human. Task 1d3322a6, owner directive
2026-07-15: customers cannot click our board — a gate whose oracle the
server can run itself must clear itself; the human stays in the loop via
visibility, reject and override, not as a required click.

SHIPS OFF BY DEFAULT (owner decision 2026-07-15, AskUserQuestion "Ship it
OFF by default"): human clicks remain the norm; an environment opts in
with PRISM_GATE_ADJUDICATOR_INTERVAL=<seconds>. The flow-entry hook in
conductor_flow honors the same switch via is_enabled().

Mirrors the other lifespan workers (watchdog / understand_drainer): one
daemon thread, env-gated. Oracle runs happen on THIS thread (they may
take minutes), which is exactly why the seat is not a maintenance-clock
pass — a slow mint must never starve the shared clock.
"""
from __future__ import annotations

import os
import sys
import threading
import time

DEFAULT_INTERVAL_S = 0  # OFF unless an environment explicitly opts in


def _interval_s() -> int:
    raw = os.environ.get("PRISM_GATE_ADJUDICATOR_INTERVAL", "")
    try:
        return int(raw) if raw.strip() else DEFAULT_INTERVAL_S
    except ValueError:
        return DEFAULT_INTERVAL_S


def _log(msg: str) -> None:
    print(f"[gate-adjudicator] {msg}", file=sys.stderr, flush=True)


def is_enabled() -> bool:
    """True when this environment opted into the seat (interval > 0).
    Consulted by the conductor_flow entry hook too, so OFF means OFF —
    neither the sweep nor the flow-entry adjudication runs."""
    return _interval_s() > 0


def sweep_once() -> list[dict]:
    """One pass over every project: adjudicate each PENDING green_gate.
    Returns the list of approvals made (empty when nothing was decidable)."""
    from prism_service.project_context import get_all_projects, get_project
    approved: list[dict] = []
    for pid in get_all_projects():
        try:
            ctx = get_project(pid)
            svc = ctx.conductor_svc
            tasks = ctx.task_svc.list()
        except Exception as exc:
            _log(f"{pid}: project unavailable ({exc})")
            continue
        for t in tasks:
            step = t.get("workflow_step") if isinstance(t, dict) \
                else getattr(t, "workflow_step", "")
            gate = t.get("gate_state") if isinstance(t, dict) \
                else getattr(t, "gate_state", "")
            tid = t.get("id") if isinstance(t, dict) else getattr(t, "id", "")
            if gate != "pending" or not tid:
                continue
            if step not in ("green_gate", "red_gate"):
                continue
            try:
                if step == "green_gate":
                    res = svc.adjudicate_green_gate(tid)
                else:
                    # red_gate: demo-proof tickets only (task 59ddfcbc);
                    # the method itself refuses everything else.
                    res = svc.adjudicate_demo_red_gate(tid)
            except Exception as exc:
                _log(f"{pid}/{tid[:8]}: adjudication raised ({exc})")
                continue
            if res and res.get("ok"):
                approved.append({"project": pid, "task_id": tid, **res})
                _log(f"{pid}/{tid[:8]}: {step} approved on machine "
                     f"evidence -> {res.get('to_step', 'advanced')}")
    return approved


def _loop(interval_s: int) -> None:
    _log(f"started; interval={interval_s}s")
    while True:
        try:
            sweep_once()
        except Exception as exc:
            _log(f"sweep error: {exc}")
        time.sleep(interval_s)


def start_gate_adjudicator() -> threading.Thread | None:
    """Spawn the adjudicator daemon thread, unless disabled via
    PRISM_GATE_ADJUDICATOR_INTERVAL=0. Mirrors start_understand_drainer."""
    interval = _interval_s()
    if interval <= 0:
        _log("disabled (default OFF; set PRISM_GATE_ADJUDICATOR_INTERVAL="
             "<seconds> to opt this environment in)")
        return None
    t = threading.Thread(target=_loop, args=(interval,),
                         name="prism-gate-adjudicator", daemon=True)
    t.start()
    return t
