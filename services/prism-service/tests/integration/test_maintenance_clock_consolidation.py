"""RED scaffold (integration) — Phase 4: fold the wall-clock memory duties
into ONE maintenance/heartbeat clock (task b4712316).

Today 4-5 independent daemon threads run the memory passes from the FastAPI
lifespan (governance memory passes, memory_ops VerifyStaleness/Forget,
adaptive policy, quality scoring). Phase 4 folds them into a SINGLE
heartbeat worker that iterates projects once per tick and runs all five
passes in sequence, each behind its own cadence gate.

These pin the REAL user-facing seams end-to-end (not a service method in
isolation — that exact gap has shipped false-greens here):

  Seam A (entrypoint)     — prism_service.services.maintenance_clock exposes
    start_maintenance_clock(interval_s=, initial_delay_s=) mirroring the
    other lifespan worker entrypoints.
  Seam B (lifespan WIRES the clock) — main.py imports start_maintenance_clock
    and spawns it (so it's not dead code).
  Seam C (lifespan RETIRES the old spawns) — the previously-separate memory
    timer spawns are gone from the lifespan: start_quality_timer is no longer
    spawned as a thread, start_memory_ops_workers() / start_adaptive_policy_worker()
    are no longer called, and start_governance_timer's memory passes are folded.
  Seam D (API reports ONE clock) — GET /api/consolidation/workers, reached
    through the REAL FastAPI router/dispatcher (TestClient(app)), lists a
    single maintenance_clock worker and NO LONGER lists the 4-5 prior memory
    timer ids (governance_timer / adaptive_policy_worker as separate memory
    entries, etc).
  Seam E (out-of-scope threads untouched) — drift_timer (Brain reindex) and
    the event pool remain wired; the fold must not disturb them.

ALL FAIL today: no maintenance_clock module/entrypoint exists, the lifespan
still spawns the separate timers, and /api/consolidation/workers still lists
governance_timer + adaptive_policy_worker as separate memory entries.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _lifespan_source() -> str:
    import prism_service.main as main_mod
    return inspect.getsource(main_mod)


# ---------------------------------------------------------------------------
# Seam A — the heartbeat entrypoint exists with the worker-lifecycle shape.
# ---------------------------------------------------------------------------

def test_start_maintenance_clock_entrypoint_exists():
    from prism_service.services import maintenance_clock as mc

    assert hasattr(mc, "start_maintenance_clock"), (
        "no start_maintenance_clock entrypoint — the consolidated clock is "
        "not a real daemon-thread entrypoint"
    )
    sig = inspect.signature(mc.start_maintenance_clock)
    assert "interval_s" in sig.parameters
    assert "initial_delay_s" in sig.parameters
    # A stable id/label the API + UI can key on.
    assert getattr(mc, "WORKER_ID", "") == "maintenance_clock", (
        "maintenance_clock must export WORKER_ID == 'maintenance_clock'"
    )


# ---------------------------------------------------------------------------
# Seam B — the lifespan WIRES the consolidated clock.
# ---------------------------------------------------------------------------

def test_main_lifespan_wires_maintenance_clock():
    src = _lifespan_source()
    assert "start_maintenance_clock" in src, (
        "main.py does not reference start_maintenance_clock — the "
        "consolidated clock is not wired into the daemon lifecycle"
    )
    assert re.search(
        r"maintenance_clock\s+import\s+start_maintenance_clock", src
    ), (
        "main.py does not import start_maintenance_clock from "
        "prism_service.services.maintenance_clock"
    )
    assert re.search(
        r"Thread\(\s*target=start_maintenance_clock[^)]*daemon=True", src, re.S
    ) or "start_maintenance_clock()" in src, (
        "start_maintenance_clock is imported but never spawned in the lifespan"
    )


# ---------------------------------------------------------------------------
# Seam C — the lifespan RETIRES the old separate memory-timer spawns.
# ---------------------------------------------------------------------------

def test_main_lifespan_retires_separate_memory_timers():
    src = _lifespan_source()
    # The quality timer must no longer be spawned as its own thread.
    assert not re.search(
        r"Thread\([^)]*target=start_quality_timer", src
    ), "start_quality_timer must no longer spawn its own thread (folded)"
    # The memory-ops worker fleet (VerifyStaleness/Forget) must no longer
    # be started independently from the lifespan.
    assert "start_memory_ops_workers()" not in src, (
        "start_memory_ops_workers() must no longer be called in the lifespan "
        "(VerifyStaleness/Forget folded into the maintenance clock)"
    )
    # The adaptive-policy worker must no longer be started independently.
    assert "start_adaptive_policy_worker()" not in src, (
        "start_adaptive_policy_worker() must no longer be called in the "
        "lifespan (folded into the maintenance clock)"
    )


# ---------------------------------------------------------------------------
# Seam E — out-of-scope threads remain wired (the fold is scoped).
# ---------------------------------------------------------------------------

def test_main_lifespan_keeps_drift_and_event_pool():
    src = _lifespan_source()
    assert "start_drift_timer" in src, (
        "drift_timer (Brain reindex) must remain — it is NOT a memory pass "
        "and is out of scope for the fold"
    )
    assert "start_event_pool" in src, (
        "the event pool (Phases 1-3) must remain wired — out of scope"
    )


# ---------------------------------------------------------------------------
# Seam D — the API reports ONE consolidated clock through the real dispatcher.
# ---------------------------------------------------------------------------

def test_workers_api_reports_single_maintenance_clock():
    from fastapi.testclient import TestClient
    from prism_service.main import app

    client = TestClient(app)
    resp = client.get("/api/consolidation/workers")
    assert resp.status_code == 200, resp.text
    workers = resp.json().get("workers", [])
    ids = [w.get("id") for w in workers]

    # Exactly one consolidated maintenance clock entry is present.
    assert ids.count("maintenance_clock") == 1, (
        f"/api/consolidation/workers must list exactly one maintenance_clock "
        f"entry; got ids={ids}"
    )

    # The prior SEPARATE memory-timer entries are gone (folded). The
    # governance_timer + adaptive_policy_worker memory entries must no longer
    # appear as standalone worker rows.
    assert "governance_timer" not in ids, (
        f"governance_timer memory-pass entry must be folded into "
        f"maintenance_clock; got ids={ids}"
    )
    assert "adaptive_policy_worker" not in ids, (
        f"adaptive_policy_worker entry must be folded into maintenance_clock; "
        f"got ids={ids}"
    )

    clock = next(w for w in workers if w.get("id") == "maintenance_clock")
    # Accurate running/cadence/description per the oracle.
    assert clock.get("running") is True, "maintenance_clock must report running"
    assert isinstance(clock.get("cadence_s"), int) and clock["cadence_s"] > 0, (
        "maintenance_clock must report a positive heartbeat cadence_s"
    )
    assert clock.get("description"), "maintenance_clock needs a description"

    # The Brain-reindex drift timer is out of scope and must survive.
    assert "drift_timer" in ids, (
        f"drift_timer must remain a separate worker (out of scope); ids={ids}"
    )
