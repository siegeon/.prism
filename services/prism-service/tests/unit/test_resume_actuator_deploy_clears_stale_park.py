"""A `deployed` signal clears a stale resume-actuator park text off any
task whose status already moved on (task a65c66e5, 2026-09-13, ops
incident, continued).

Live regression: task a65c66e5's real history shows a bare status PATCH
('blocked' -> 'in_progress', no blocked_reason kwarg, no
resume_actuator_released row) that left the OLD park text
("resume-actuator: retry budget spent (3/3) -- parked for a human")
sitting on blocked_reason forever -- the card read "still parked" even
though the task was genuinely driving again. TaskService.update now
auto-clears blocked_reason on any FUTURE such transition, but that does
nothing for a row already stuck in this state. `_clear_stale_park_text`,
run on a `deployed` wakeup, sweeps every in_progress task and clears any
blocked_reason this seat itself wrote.
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

_LEGACY_PARK = ("resume-actuator: retry budget spent (3/3) — parked for a "
               "human")


def _project():
    return "resume-deploy-" + uuid.uuid4().hex[:8]


def test_stale_park_text_on_an_in_progress_task_clears_on_force(monkeypatch):
    from prism_service.project_context import get_project
    from prism_service.services import resume_actuator as ra

    project = _project()
    ctx = get_project(project)
    task = ctx.task_svc.create(title="a65c66e5-shaped task")
    # Exactly the live shape: status already moved on, blocked_reason did
    # not (a raw PATCH, not this seat's own release()).
    ctx.task_svc.update(task.id, status="blocked", blocked_reason=_LEGACY_PARK)
    ctx.task_svc._db.execute(
        "UPDATE tasks SET status='in_progress' WHERE id=?", (task.id,))
    ctx.task_svc._db.commit()
    assert ctx.task_svc.get(task.id).status == "in_progress"
    assert ctx.task_svc.get(task.id).blocked_reason == _LEGACY_PARK

    cleared = ra._clear_stale_park_text(project)

    assert cleared == [task.id]
    after = ctx.task_svc.get(task.id)
    assert after.blocked_reason == "", after.blocked_reason
    rows = [h for h in ctx.task_svc.history(task.id)
           if h.action == ra.RELEASED_ACTION]
    assert rows, "clearing a stale park must leave an audit row"
    assert rows[-1].actor == ra.SEAT


def test_an_unrelated_or_already_blocked_reason_is_left_alone(monkeypatch):
    from prism_service.project_context import get_project
    from prism_service.services import resume_actuator as ra

    project = _project()
    ctx = get_project(project)

    still_blocked = ctx.task_svc.create(title="genuinely still blocked")
    ctx.task_svc.update(still_blocked.id, status="blocked",
                        blocked_reason=_LEGACY_PARK)

    unrelated = ctx.task_svc.create(title="unrelated dependency block")
    ctx.task_svc.update(unrelated.id, status="blocked",
                        blocked_reason="waiting on a dependency task")
    ctx.task_svc._db.execute(
        "UPDATE tasks SET status='in_progress' WHERE id=?", (unrelated.id,))
    ctx.task_svc._db.commit()

    cleared = ra._clear_stale_park_text(project)

    assert unrelated.id not in cleared, (
        "a reason this seat never wrote must never be cleared")
    assert ctx.task_svc.get(unrelated.id).blocked_reason == (
        "waiting on a dependency task")
    assert ctx.task_svc.get(still_blocked.id).blocked_reason == _LEGACY_PARK, (
        "a task genuinely still blocked is _rearm_once's job, not this "
        "sweep's")


def test_sweep_once_for_with_force_clears_stale_text_before_anything_else(
        monkeypatch):
    """The end-to-end wiring: sweep_once_for(force=True) calls the clear,
    whether or not a dispatch/rearm follows."""
    from prism_service.project_context import get_project
    from prism_service.services import resume_actuator as ra
    from prism_service.services import task_runner as _runner

    project = _project()
    ctx = get_project(project)
    task = ctx.task_svc.create(title="stale on force")
    ctx.task_svc.update(task.id, status="blocked", blocked_reason=_LEGACY_PARK)
    ctx.task_svc._db.execute(
        "UPDATE tasks SET status='in_progress' WHERE id=?", (task.id,))
    ctx.task_svc._db.commit()
    # Nothing else eligible -- isolate the clear from any dispatch path.
    monkeypatch.setattr(_runner, "_engine_unreachable", lambda: True)

    ra.sweep_once_for(project, force=True)

    assert ctx.task_svc.get(task.id).blocked_reason == ""


# ---------------------------------------------------------------------------
# Second round, task a65c66e5 (2026-09-13): same fix as gate_adjudicator's --
# a `deployed` signal fired at API startup is invisible to a wait baseline
# taken after warmup, so a fresh worker host's FIRST pass must force
# regardless of any signal.
# ---------------------------------------------------------------------------


class _StopLoop(Exception):
    pass


def test_the_first_pass_after_warmup_is_unconditional_with_no_signals(
        monkeypatch):
    from prism_service.services import resume_actuator as ra
    from prism_service.services import wakeups as wk

    wk._reset_for_tests()
    forces: list[bool] = []

    def fake_sweep_once(force=False):
        forces.append(force)
        return None

    wait_calls = {"n": 0}

    def fake_wait(kinds, timeout=None):
        wait_calls["n"] += 1
        if wait_calls["n"] >= 2:
            raise _StopLoop()
        return True

    monkeypatch.setattr(ra, "sweep_once", fake_sweep_once)
    monkeypatch.setattr(wk, "wait_out_startup_warmup", lambda: None)
    monkeypatch.setattr(wk, "lower_thread_priority", lambda: None)
    monkeypatch.setattr(wk, "wait", fake_wait)
    monkeypatch.setattr(wk, "worker_fallback_s", lambda: None)

    with pytest.raises(_StopLoop):
        ra._loop(60)

    assert forces == [True, False], (
        f"pass 1 (post-warmup, no signals at all) must force; pass 2 (no "
        f"new signal since) must not -- got {forces}")
