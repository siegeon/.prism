"""GET /api/work/graph must not re-pay the per-call timeout on every poll
for a session whose transcript read is already in flight from an earlier
poll (owner 2026-09-13, live: total_ms flat at ~0.373-0.411s -- exactly
_GRAPH_CALL_TIMEOUT_S -- on EVERY warm poll of /api/work/graph, never
dropping, on real data).

Round-1 (test_work_graph_transcript_io_budget.py) bounded each poll's own
wait. Round-2 (test_work_graph_no_thread_pileup_across_polls.py) deduped
the background SUBMISSION so N polls don't spawn N threads. Neither
caught this: even a deduped, reused future still made every poll block
for a FRESH `min(_GRAPH_CALL_TIMEOUT_S, remaining)` wait on it, so a
chronically-slow session (slower than the timeout, which is exactly what
triggers this path at all) cost every single poll the same flat ~0.4s
forever -- reproduced directly against `_bounded` before this fix: two
back-to-back calls for the identical (fn, args) key both measured
~400ms. This test pins the fix: only the poll that actually SUBMITS the
call pays the timeout; a poll that only reused someone else's in-flight
future returns near-instantly."""

from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def test_second_poll_for_an_inflight_call_does_not_repay_the_timeout(monkeypatch):
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "_GRAPH_CALL_TIMEOUT_S", 0.4)
    calls = {"n": 0}

    def slow(*_a, **_k):
        calls["n"] += 1
        time.sleep(2.0)  # slower than the timeout, on purpose
        return "value"

    deadline = time.monotonic() + 1.5

    t0 = time.monotonic()
    _val1, ok1 = work_api._bounded(deadline, slow, "same-session")
    t1 = time.monotonic()
    _val2, ok2 = work_api._bounded(deadline, slow, "same-session")
    t2 = time.monotonic()

    first_ms = (t1 - t0) * 1000
    second_ms = (t2 - t1) * 1000

    assert ok1 is False and ok2 is False  # neither poll waits for the 2s call
    assert calls["n"] == 1, "dedup must still hold: only one real call made"
    assert first_ms >= 350, (
        f"the FIRST (fresh-submit) poll should still pay close to the "
        f"{work_api._GRAPH_CALL_TIMEOUT_S * 1000:.0f}ms per-call timeout, "
        f"got {first_ms:.1f}ms"
    )
    assert second_ms < 50, (
        f"a poll that only REUSES an already-in-flight call must not "
        f"re-wait the per-call timeout -- got {second_ms:.1f}ms, expected "
        f"near-zero (this is the live defect: every poll paid a flat "
        f"~400ms == _GRAPH_CALL_TIMEOUT_S on real data because this wait "
        f"was being repaid every single request)"
    )
