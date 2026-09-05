"""An oscillating task hits an absolute dispatch ceiling no reset can
lift (task 338f7810, 2026-09-05).

7.13.233 taught `sweep_once_for` to reset a spent retry budget when a
conductor transition landed after the last charged attempt. That is right
for a task that genuinely moved on — but it removed the only backstop for
a task that OSCILLATES.

LIVE REGRESSION, and a regression from that very fix: task 338f7810
advanced, was refused, rewound and advanced again, over and over. Every
sweep saw a transition newer than the last attempt, reset the budget, and
dispatched again — 37 dispatches over 4 hours 40 minutes, never parking.
The owner saw it as "stuck in some type of cycle" with "hundreds of
attempts".

The per-pass budget still governs the ordinary case. This ceiling counts
every dispatch the seat has EVER made for the task, from durable history,
so a task that keeps producing transitions still stops. Its park message
is deliberately different: the task kept MOVING and still never finished,
which is a different problem from a step that never advanced once.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture()
def task():
    from prism_service.project_context import get_project

    project = "ceiling-" + uuid.uuid4().hex[:8]
    ctx = get_project(project)
    t = ctx.task_svc.create(title="oscillating task")
    ctx.task_svc.update(t.id, status="in_progress",
                        workflow_step="implement_tasks")
    return ctx, t.id, project


def _dispatches(ctx, task_id: str, n: int) -> None:
    from prism_service.services import resume_actuator as ra

    for _ in range(n):
        ctx.task_svc.record_history(
            task_id, action=ra.DISPATCH_ACTION,
            details="seat=prism-resume-actuator; step=implement_tasks",
            actor=ra.SEAT)


def test_total_dispatches_counts_durable_history(task):
    from prism_service.services import resume_actuator as ra

    ctx, task_id, project = task
    _dispatches(ctx, task_id, 5)

    assert ra._total_dispatches(project, task_id) == 5


def test_a_task_at_the_ceiling_parks_even_though_it_keeps_advancing(
        task, monkeypatch):
    """The live 338f7810 shape: transitions keep landing, so the per-pass
    budget resets every sweep — the ceiling must stop it anyway."""
    from prism_service.services import resume_actuator as ra

    ctx, task_id, project = task
    _dispatches(ctx, task_id, ra._max_total_dispatches())
    # It IS advancing: the reset path would fire without the ceiling.
    monkeypatch.setattr(ra, "_advanced_since", lambda p, t, s: True)
    monkeypatch.setattr(ra, "_open_retry_task_id", lambda p: task_id)
    monkeypatch.setattr(
        ra, "dispatch_once",
        lambda p, t: (_ for _ in ()).throw(
            AssertionError("must not dispatch past the ceiling")))

    res = ra.sweep_once_for(project)

    assert res.get("parked") is True
    assert res.get("looping") is True
    assert ctx.task_svc.get(task_id).status == "blocked"


def test_the_park_reason_says_it_kept_moving(task, monkeypatch):
    """A person must be able to tell this from a step that never advanced
    once — different problem, different message."""
    from prism_service.services import resume_actuator as ra

    ctx, task_id, project = task
    _dispatches(ctx, task_id, ra._max_total_dispatches())
    monkeypatch.setattr(ra, "_advanced_since", lambda p, t, s: True)
    monkeypatch.setattr(ra, "_open_retry_task_id", lambda p: task_id)
    monkeypatch.setattr(ra, "dispatch_once", lambda p, t: {"ok": True})

    res = ra.sweep_once_for(project)
    reason = res.get("reason") or ""

    assert "ceiling" in reason
    assert "without reaching a terminal state" in reason
    assert "budget spent" not in reason, (
        "the spent-budget message describes a different failure")


def test_below_the_ceiling_still_dispatches(task, monkeypatch):
    """The ceiling is a backstop, not a new limit on ordinary retries."""
    from prism_service.services import resume_actuator as ra

    ctx, task_id, project = task
    _dispatches(ctx, task_id, ra._max_total_dispatches() - 1)
    monkeypatch.setattr(ra, "_advanced_since", lambda p, t, s: True)
    monkeypatch.setattr(ra, "_open_retry_task_id", lambda p: task_id)
    seen: list[str] = []
    monkeypatch.setattr(ra, "dispatch_once",
                        lambda p, t: seen.append(t) or {"ok": True})

    ra.sweep_once_for(project)

    assert seen == [task_id]
    assert ctx.task_svc.get(task_id).status == "in_progress"


def test_the_ceiling_is_configurable(monkeypatch):
    from prism_service.services import resume_actuator as ra

    monkeypatch.setenv("PRISM_RESUME_ACTUATOR_MAX_TOTAL", "40")
    assert ra._max_total_dispatches() == 40
    monkeypatch.setenv("PRISM_RESUME_ACTUATOR_MAX_TOTAL", "nonsense")
    assert ra._max_total_dispatches() == ra.DEFAULT_MAX_TOTAL_DISPATCHES
