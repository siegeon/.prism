"""RED scaffold (integration) — Phase 5 (epic 4fd1e6b4, task 034fbd6c):
collapse the Background Activity memory concern into ~2 pipeline rows +
1 maintenance-clock row.

Today GET /api/consolidation/workers (api/consolidation.py:43-211) still
returns the pre-consolidation shape: discrete reflection_worker +
memory_summary_worker rows plus a maintenance_clock row — the memory
concern is spread across three+ discrete worker rows rather than presented
as ~2 PIPELINE rows (event throughput) + 1 CLOCK row.

These pin the REAL user-facing seams end-to-end (not a service method in
isolation — that exact gap has shipped false-greens in this epic):

  Seam A (EventBus metrics)   — event_pool.EventBus exposes the NEW metrics
    the event pipeline row needs: events/min throughput, last-event
    timestamp, in-flight count. Today only queue_depth() (event_pool.py:211)
    exists.
  Seam B (API emits pipeline rows) — GET /api/consolidation/workers, reached
    through the REAL FastAPI dispatcher (TestClient(app)), emits at least one
    row of kind=="pipeline" carrying events_per_min / queue_depth /
    last_event_ts / in_flight, sourced from the live bus.
  Seam C (API emits a clock row)   — the same endpoint emits a row of
    kind=="clock" carrying interval_s / last_sweep / next_sweep, sourced
    from maintenance_clock.
  Seam D (memory concern collapsed) — the discrete memory worker rows
    (reflection_worker, memory_summary_worker) NO LONGER appear as
    standalone rows; the memory concern is represented by the pipeline +
    clock rows instead.
  Seam E (non-memory infra survives) — transcript_importer / drift_timer /
    trash_sweeper / auto_updater / understand_drainer stay listed
    separately (out of scope for the collapse).

ALL FAIL today: EventBus has no throughput/last-event/in-flight metrics,
the API emits no kind field / pipeline / clock rows, and it still lists
reflection_worker + memory_summary_worker as discrete rows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# Seam A — EventBus exposes the NEW metrics the event pipeline row needs.
# ---------------------------------------------------------------------------

def test_event_bus_exposes_throughput_lastevent_inflight():
    from prism_service.services import event_pool as ep

    bus = ep.EventBus()

    # events/min (rate) — a numeric throughput readout, not just queue_depth.
    assert hasattr(bus, "events_per_min"), (
        "EventBus must expose events_per_min() — the pipeline row reports "
        "event throughput, which does not exist today (only queue_depth)"
    )
    rate = bus.events_per_min()
    assert isinstance(rate, (int, float)) and rate >= 0

    # last-event timestamp — None until an event flows, then a real epoch.
    assert hasattr(bus, "last_event_ts"), (
        "EventBus must expose last_event_ts() for the pipeline 'last event' "
        "readout"
    )
    assert bus.last_event_ts() is None

    # in-flight count — events claimed but not yet finished dispatching.
    assert hasattr(bus, "in_flight"), (
        "EventBus (or the pool) must expose in_flight() for the pipeline "
        "'in-flight' readout"
    )
    assert isinstance(bus.in_flight(), int)


def test_last_event_ts_advances_on_emit():
    from prism_service.services import event_pool as ep

    bus = ep.EventBus()
    assert bus.last_event_ts() is None, "no events yet → no last-event stamp"
    bus.emit(ep.Event(type=ep.MEMORY_WRITTEN, payload={}))
    ts = bus.last_event_ts()
    assert ts is not None and ts > 0, (
        "emit() must stamp last_event_ts so the pipeline row can show "
        "'last event Xs ago'"
    )


# ---------------------------------------------------------------------------
# Seam B — the API emits a PIPELINE row through the real dispatcher.
# ---------------------------------------------------------------------------

def _workers():
    from fastapi.testclient import TestClient
    from prism_service.main import app

    client = TestClient(app)
    resp = client.get("/api/consolidation/workers")
    assert resp.status_code == 200, resp.text
    return resp.json().get("workers", [])


def test_workers_api_emits_event_pipeline_row():
    workers = _workers()
    pipelines = [w for w in workers if w.get("kind") == "pipeline"]
    assert pipelines, (
        "/api/consolidation/workers must emit at least one kind=='pipeline' "
        f"row for the event pipeline; got kinds="
        f"{[w.get('kind') for w in workers]}"
    )
    pipe = pipelines[0]
    # The four readouts the oracle requires on a pipeline row.
    for field in ("events_per_min", "queue_depth", "last_event_ts", "in_flight"):
        assert field in pipe, (
            f"pipeline row missing '{field}' — needs event rate + queue depth "
            f"+ last-event ts + in-flight count; got keys={sorted(pipe)}"
        )
    assert isinstance(pipe["queue_depth"], int)
    assert isinstance(pipe["in_flight"], int)


# ---------------------------------------------------------------------------
# Seam C — the API emits a CLOCK row sourced from maintenance_clock.
# ---------------------------------------------------------------------------

def test_workers_api_emits_maintenance_clock_row():
    workers = _workers()
    clocks = [w for w in workers if w.get("kind") == "clock"]
    assert clocks, (
        "/api/consolidation/workers must emit a kind=='clock' row for the "
        f"maintenance clock; got kinds={[w.get('kind') for w in workers]}"
    )
    clock = clocks[0]
    for field in ("interval_s", "last_sweep", "next_sweep"):
        assert field in clock, (
            f"clock row missing '{field}' — must report interval + last sweep "
            f"+ next sweep; got keys={sorted(clock)}"
        )
    assert isinstance(clock["interval_s"], int) and clock["interval_s"] > 0


# ---------------------------------------------------------------------------
# Seam D — the discrete memory worker rows are COLLAPSED (gone).
# ---------------------------------------------------------------------------

def test_discrete_memory_worker_rows_are_collapsed():
    workers = _workers()
    ids = [w.get("id") for w in workers]
    assert "reflection_worker" not in ids, (
        f"reflection_worker must be folded into the event pipeline + clock "
        f"rows, not listed as a discrete row; got ids={ids}"
    )
    assert "memory_summary_worker" not in ids and "memory_summary_entry" not in ids, (
        f"memory_summary_worker must be folded into the pipeline/clock rows; "
        f"got ids={ids}"
    )


# ---------------------------------------------------------------------------
# Seam E — out-of-scope infra/brain workers remain listed separately.
# ---------------------------------------------------------------------------

def test_non_memory_infra_workers_survive():
    ids = [w.get("id") for w in _workers()]
    for survivor in (
        "transcript_importer", "drift_timer", "trash_sweeper",
        "auto_updater", "understand_drainer",
    ):
        assert survivor in ids, (
            f"{survivor} is non-memory infra and must remain a separate row; "
            f"got ids={ids}"
        )
