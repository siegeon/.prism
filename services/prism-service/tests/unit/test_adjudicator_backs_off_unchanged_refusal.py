"""The sweep must not re-attempt a refusal that cannot change.

Measured on prism/tasks.db 2026-08-29: ten tasks parked at green_gate,
~19,000 task_history rows and ~10 MB per DAY, 110 MB total. Three of them
carry proof_type='demo', which adjudicate_green_gate refuses to decide BY
DESIGN (human-only, owner rule eaafdf75) -- so the sweep re-attempted a
verdict that cannot exist 1,440 times a day, each attempt writing rows.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import gate_adjudicator as ga  # noqa: E402


class _T:
    def __init__(self, updated_at="t0"):
        self.updated_at = updated_at


def setup_function(_):
    ga._BACKOFF.clear()


def test_an_unchanged_refusal_is_not_re_attempted_next_sweep():
    t = _T()
    assert ga._backoff_should_skip("x", t) is False, "first pass must run"
    ga._backoff_note_refused("x", t)
    assert ga._backoff_should_skip("x", t) is True, (
        "a refusal that has not changed must not be re-attempted immediately "
        "-- this is the 1,440-attempts-a-day loop")


def test_the_delay_doubles_and_is_capped():
    t = _T()
    seen = []
    for _ in range(12):
        ga._backoff_note_refused("x", t)
        seen.append(ga._BACKOFF["x"][2])
    assert seen[0] == 60.0 and seen[1] == 120.0 and seen[2] == 240.0, seen
    assert max(seen) == ga._BACKOFF_CAP_S, f"delay must cap, got {max(seen)}"


def test_a_task_that_actually_changed_is_re_attempted_at_once():
    """A minted receipt, a push or a human edit moves updated_at. The backoff
    may delay REPETITION, never a real decision."""
    t = _T("t0")
    ga._backoff_note_refused("x", t)
    assert ga._backoff_should_skip("x", t) is True
    moved = _T("t1")
    assert ga._backoff_should_skip("x", moved) is False, (
        "the task changed -- the seat must look again on this very sweep")
    assert "x" not in ga._BACKOFF, "a changed task must reset its backoff"


def test_an_approval_clears_the_backoff():
    t = _T()
    ga._backoff_note_refused("x", t)
    ga._backoff_clear("x")
    assert ga._backoff_should_skip("x", t) is False


def test_the_backoff_expires_on_its_own():
    t = _T()
    ga._backoff_note_refused("x", t)
    tid_updated, _next_at, delay = ga._BACKOFF["x"]
    ga._BACKOFF["x"] = (tid_updated, time.monotonic() - 1.0, delay)
    assert ga._backoff_should_skip("x", t) is False, (
        "once the delay elapses the seat must try again")
