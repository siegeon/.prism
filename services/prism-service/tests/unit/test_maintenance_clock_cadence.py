"""RED scaffold (unit) — Phase 4: the maintenance clock runs the five
memory passes in sequence per tick, each behind its OWN independent
cadence gate (task b4712316).

The behavioral contract:

  * One tick iterates and runs the passes in a FIXED sequence:
    governance (TTL+decay+duplicate), verify_staleness, forget,
    adaptive (retune), quality (vs git truth).
  * Each pass has its own cadence — a pass only runs when ITS interval has
    elapsed since its last run. On a fresh clock all due passes run; a second
    tick taken before any interval elapses runs NONE of them again.
  * The per-pass env gates/cadence overrides remain honored
    (PRISM_GOVERNANCE_INTERVAL, PRISM_QUALITY_INTERVAL,
    PRISM_ADAPTIVE_POLICY_WORKER[_INTERVAL], PRISM_<OP>_WORKER for
    verify_staleness/forget).

FAILS today: prism_service.services.maintenance_clock does not exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _import_clock():
    from prism_service.services import maintenance_clock as mc
    return mc


# Canonical ordered set of passes the clock folds.
_EXPECTED_PASSES = ["governance", "verify_staleness", "forget", "adaptive", "quality"]


def test_clock_defines_the_five_passes_in_order():
    mc = _import_clock()
    passes = mc.PASS_ORDER if hasattr(mc, "PASS_ORDER") else None
    assert passes is not None, (
        "maintenance_clock must expose PASS_ORDER — the ordered list of the "
        "five folded memory passes"
    )
    names = [getattr(p, "name", p) for p in passes]
    assert names == _EXPECTED_PASSES, (
        f"PASS_ORDER must be exactly {_EXPECTED_PASSES} in that sequence; "
        f"got {names}"
    )


def test_each_pass_has_independent_cadence_gate(monkeypatch):
    """A pass runs only when its interval has elapsed; a second immediate
    tick runs none of them again (independent per-pass cadence gates)."""
    mc = _import_clock()

    # A controllable clock so we drive elapsed time deterministically.
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(mc, "_now", lambda: fake_now["t"], raising=False)

    ran: list[str] = []
    # Stub each pass body so we observe which fired, not real side effects.
    for name in _EXPECTED_PASSES:
        monkeypatch.setattr(
            mc, f"_run_{name}",
            (lambda n: (lambda project: ran.append(n)))(name),
            raising=False,
        )

    state = mc.new_clock_state()

    # First tick on a fresh clock: every due pass fires once.
    mc.run_tick("prism", state)
    assert ran == _EXPECTED_PASSES, (
        f"first tick must run all five passes in order; got {ran}"
    )

    # Second tick with NO time elapsed: no cadence has re-elapsed, so none fire.
    ran.clear()
    mc.run_tick("prism", state)
    assert ran == [], (
        f"a tick before any interval elapses must run NO passes again; got {ran}"
    )


def test_quality_cadence_longer_than_adaptive(monkeypatch):
    """Per-pass cadences differ — quality (~6h) must be a much longer gate
    than adaptive (~1h), proving the gates are independent not uniform."""
    mc = _import_clock()
    monkeypatch.delenv("PRISM_QUALITY_INTERVAL", raising=False)
    monkeypatch.delenv("PRISM_ADAPTIVE_POLICY_WORKER_INTERVAL", raising=False)

    cadences = mc.pass_cadences()
    assert cadences["quality"] > cadences["adaptive"], (
        f"quality cadence must exceed adaptive cadence; got {cadences}"
    )
    # Defaults track config: quality 6h, adaptive 1h.
    assert cadences["quality"] == 21600
    assert cadences["adaptive"] == 3600


def test_env_overrides_honored_for_cadence(monkeypatch):
    mc = _import_clock()
    monkeypatch.setenv("PRISM_QUALITY_INTERVAL", "111")
    monkeypatch.setenv("PRISM_ADAPTIVE_POLICY_WORKER_INTERVAL", "222")
    monkeypatch.setenv("PRISM_GOVERNANCE_INTERVAL", "333")
    cadences = mc.pass_cadences()
    assert cadences["quality"] == 111, "PRISM_QUALITY_INTERVAL must be honored"
    assert cadences["adaptive"] == 222, (
        "PRISM_ADAPTIVE_POLICY_WORKER_INTERVAL must be honored"
    )
    assert cadences["governance"] == 333, (
        "PRISM_GOVERNANCE_INTERVAL must be honored"
    )
