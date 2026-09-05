"""A gate node's bar means "how close to PASSING", not "how many ran"
(owner 2026-09-04: "the progress is not progression, it's still stuck at
full ... it should be filling up as we go, not showing full from the
onset").

`progress_source` counted every ANSWERED tooth toward a gate node's fill.
Every tooth answers within a second of the gate opening, so the bar was
full immediately and stayed full for the entire wait — and a FAILED tooth
counted toward it too.

LIVE REGRESSION: task 338f7810 sat at plan_gate reading
`{'basis': 'teeth', 'done': 3, 'total': 3}` — a full bar — while its
teeth were:
    absent_file_claim -> passed
    stop_if_pinned    -> passed
    already_green_ac  -> FAILED
A refusing gate rendered as complete. That is the "reads as done" misfire
this repo has already been bitten by.

`done` now counts teeth that PASSED, and `answered`/`failed` ride along so
a caller can say why the bar stopped short instead of leaving it
unexplained.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

STEP = "plan_gate"


def _source(monkeypatch, teeth):
    from prism_service.services import flow_run_recorder as fr

    monkeypatch.setattr(fr, "gate_teeth", lambda p, t, s: teeth)
    return fr.progress_source("unused-db", "t-1", STEP, project="proj")


def test_a_failed_tooth_does_not_fill_the_bar(monkeypatch):
    """The live 338f7810 shape: 2 passed, 1 failed, all answered."""
    got = _source(monkeypatch, [
        {"id": "absent_file_claim", "status": "passed"},
        {"id": "stop_if_pinned", "status": "passed"},
        {"id": "already_green_ac", "status": "failed"},
    ])

    assert got["total"] == 3
    assert got["done"] == 2, (
        "a failed tooth must not count toward the fill — a refusing gate "
        f"cannot read as complete; got {got}")
    assert got["failed"] == 1
    assert got["answered"] == 3
    assert got["done"] / got["total"] < 1.0


def test_an_all_passing_gate_reads_complete(monkeypatch):
    got = _source(monkeypatch, [
        {"id": "a", "status": "passed"},
        {"id": "b", "status": "passed"},
    ])

    assert got["done"] == got["total"] == 2
    assert got["failed"] == 0


def test_an_undecided_tooth_is_not_progress(monkeypatch):
    """A tooth still waiting has neither passed nor failed — it must not
    fill the bar, which is what made the gate read done on arrival."""
    got = _source(monkeypatch, [
        {"id": "a", "status": "passed"},
        {"id": "b", "status": "pending"},
        {"id": "c", "status": ""},
    ])

    assert got["done"] == 1
    assert got["answered"] == 1
    assert got["failed"] == 0


def test_a_refused_tooth_counts_as_answered_but_not_passing(monkeypatch):
    got = _source(monkeypatch, [
        {"id": "a", "status": "refused"},
        {"id": "b", "status": "passed"},
    ])

    assert got["done"] == 1
    assert got["answered"] == 2
    assert got["failed"] == 1


def test_a_gate_with_no_teeth_reports_nothing_rather_than_full(monkeypatch):
    got = _source(monkeypatch, [])

    assert got["total"] == 0
    assert got["done"] == 0
