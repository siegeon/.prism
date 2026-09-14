"""The engine-slot-freed wake (task 8ddbba7f follow-up, 2026-09-13).

Live incident: task bb3d1f6a was rewound to write_failing_tests (a
non-gate, in_progress step) while the engine's one slot was held by a
different task's drive. task_runner's own sweep correctly skipped it
(dispatch_guard.engine_slot_reason busy), but nothing ever told the sweep
to look again once that OTHER drive finished -- dispatch_guard.
end_dispatch freed the slot in _OPEN_TICKETS but signalled nobody, and
task_runner._loop's _wake_event only fires from task_service.py's own
task.changed publish (a real column write), which the freed slot itself
never causes. bb3d1f6a was never attempted again; only an unrelated
task_changed write, or resume_actuator's 180s stall sweep, could have
rescued it.

Two independent fixes, verified live in dispatch_guard.py/task_runner.py
before writing this:
  - THE SIGNAL -- dispatch_guard.end_dispatch (the ONE place every real
    dispatch, from every seat, ends -- task_runner.run_one_step's success
    and exception branches, resume_actuator.dispatch_once's success and
    exception branches) now wakes task_runner's drive loop directly, the
    same call task_service.py's own task.changed publish already makes.
  - THE BELT-AND-BRACES -- eligible_tasks() records, in a module global
    mirroring gate_adjudicator's _last_eligible_count pattern, whether it
    skipped a candidate this pass because the engine slot was busy;
    sweep_once() resets that flag every tick; _loop's wait timeout is
    bounded (_ENGINE_BUSY_RETRY_S) whenever the flag is set, instead of
    blocking indefinitely, in case the signal above is ever missed. A
    quiet tick with nothing eligible must still block indefinitely --
    that reactive guarantee (task 7.13.357) must not regress.

  AC-1 -- end_dispatch wakes task_runner, on a normal close.
  AC-2 -- end_dispatch wakes task_runner even when the caller reached it
          from an exception handler (dispatch_guard has no branch on why
          it was called -- proven by the identical call shape).
  AC-3 -- end_dispatch(None) (a refused dispatch never opened a ticket)
          wakes nobody -- nothing was freed.
  AC-4 -- eligible_tasks() records a busy-skip when a candidate is
          skipped for "engine slot busy".
  AC-5 -- eligible_tasks() does NOT clear a stale busy-skip when
          everything eligible was actually returned -- clearing is
          sweep_once()'s job, once per tick, not eligible_tasks()'s.
  AC-6 -- sweep_once() resets the busy-skip flag every tick, even when it
          finds no projects/candidates at all.
  AC-7 -- _wait_timeout_s bounds the wait to _ENGINE_BUSY_RETRY_S when
          busy_skip is set and the underlying fallback is None (the
          default, reactive-only contract).
  AC-8 -- _wait_timeout_s leaves an explicit, SHORTER operator fallback
          alone -- it only ever narrows the wait, never lengthens it.
  AC-9 -- _wait_timeout_s returns the untouched fallback when busy_skip
          is False -- the reactive guarantee itself.
  AC-10 -- the real _loop wires the flag through: a sweep that defers for
           a busy slot makes the loop's own wait bounded.
"""
from __future__ import annotations

import threading
import time
import types
import uuid

import pytest


def _project() -> str:
    return "engine-slot-freed-" + uuid.uuid4().hex[:8]


@pytest.fixture()
def make_task():
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(project, **kwargs):
        ctx = get_project(project)
        task = ctx.task_svc.create(
            title=kwargs.pop("title", "engine slot freed task"), **kwargs)
        ctx.task_svc.update(task.id, status="in_progress",
                            workflow_step="implement_tasks")
        created.append(task.id)
        return ctx, ctx.task_svc.get(task.id)

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


# ---------------------------------------------------------------------------
# AC-1 / AC-2 / AC-3 -- the signal
# ---------------------------------------------------------------------------

def test_end_dispatch_wakes_task_runner(make_task, monkeypatch):
    from prism_service.services import dispatch_guard as dg
    from prism_service.services import task_runner as tr

    woke = []
    monkeypatch.setattr(tr, "wake", lambda: woke.append(True))

    project = _project()
    ctx, task = make_task(project)
    woke.clear()  # make_task's own status=in_progress update already woke it

    ticket, reason = dg.try_begin(project, task.id, "implement_tasks", "seat")
    assert ticket is not None, reason
    assert woke == [], "try_begin must not itself wake anyone"

    dg.end_dispatch(ticket)

    assert woke == [True], (
        "closing a ticket frees the engine slot -- exactly the event a "
        "task skipped for 'engine slot busy' is waiting on, and must "
        "wake task_runner's drive loop")


def test_end_dispatch_wakes_task_runner_from_an_exception_handler(
        make_task, monkeypatch):
    """dispatch_guard.end_dispatch has no branch on WHY it is being
    called -- both real call sites (task_runner.run_one_step,
    resume_actuator.dispatch_once) call it identically from their success
    path and their `except Exception:` path. This proves the exception
    shape explicitly rather than trusting that symmetry by inspection."""
    from prism_service.services import dispatch_guard as dg
    from prism_service.services import task_runner as tr

    woke = []
    monkeypatch.setattr(tr, "wake", lambda: woke.append(True))

    project = _project()
    ctx, task = make_task(project)
    woke.clear()  # make_task's own status=in_progress update already woke it

    ticket, reason = dg.try_begin(project, task.id, "implement_tasks", "seat")
    assert ticket is not None, reason
    woke.clear()  # try_begin's own DISPATCH_ACTION history row too

    try:
        raise RuntimeError("simulated claude_cli.invoke failure")
    except RuntimeError:
        dg.end_dispatch(ticket)

    assert woke == [True]


def test_end_dispatch_of_none_wakes_nobody(monkeypatch):
    from prism_service.services import dispatch_guard as dg
    from prism_service.services import task_runner as tr

    woke = []
    monkeypatch.setattr(tr, "wake", lambda: woke.append(True))

    dg.end_dispatch(None)

    assert woke == [], (
        "a refused dispatch never opened a ticket, so closing None frees "
        "nothing and must not fire the signal")


# ---------------------------------------------------------------------------
# AC-4 / AC-5 / AC-6 -- the belt-and-braces flag
# ---------------------------------------------------------------------------

def test_eligible_tasks_records_a_busy_skip(monkeypatch):
    from prism_service.services import task_runner as tr
    from prism_service.services import dispatch_guard as dg

    monkeypatch.setattr(tr, "_foreign_driver_on", lambda p, tid: "")
    monkeypatch.setattr(
        dg, "engine_slot_reason",
        lambda project, exclude_task_id="": "engine slot busy: occupant")

    class _T:
        def __init__(self, tid, step):
            self.id, self.workflow_step = tid, step

    class _Svc:
        def list(self, **_kw):
            return [_T("candidate", "implement_tasks")]

    monkeypatch.setattr(
        "prism_service.project_context.get_project",
        lambda p: types.SimpleNamespace(task_svc=_Svc()))

    tr._last_engine_busy_skip = False
    assert tr.eligible_tasks("p", 5) == []
    assert tr._last_engine_busy_skip is True


def test_eligible_tasks_does_not_clear_a_stale_flag_itself(monkeypatch):
    from prism_service.services import task_runner as tr
    from prism_service.services import dispatch_guard as dg

    monkeypatch.setattr(tr, "_foreign_driver_on", lambda p, tid: "")
    monkeypatch.setattr(
        dg, "engine_slot_reason", lambda project, exclude_task_id="": None)

    class _T:
        def __init__(self, tid, step):
            self.id, self.workflow_step = tid, step

    class _Svc:
        def list(self, **_kw):
            return [_T("candidate", "implement_tasks")]

    monkeypatch.setattr(
        "prism_service.project_context.get_project",
        lambda p: types.SimpleNamespace(task_svc=_Svc()))

    tr._last_engine_busy_skip = True  # stale from a prior tick
    assert tr.eligible_tasks("p", 5) == ["candidate"]
    assert tr._last_engine_busy_skip is True, (
        "eligible_tasks() only ever SETS the flag on a real busy-skip -- "
        "clearing a stale value each tick is sweep_once()'s job, not "
        "eligible_tasks()'s")


def test_sweep_once_resets_the_busy_skip_flag_every_tick(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.setattr(tr, "_spend_ceiling_crossed", lambda: False)
    monkeypatch.setattr(tr, "_system_overloaded", lambda: False)
    monkeypatch.setattr(tr, "_engine_unreachable", lambda: False)
    monkeypatch.setattr(
        "prism_service.project_context.get_all_projects", lambda: [])

    tr._last_engine_busy_skip = True
    assert tr.sweep_once() is None
    assert tr._last_engine_busy_skip is False, (
        "a stale busy-skip from an earlier tick must never survive into "
        "a tick that found no projects/candidates at all")


# ---------------------------------------------------------------------------
# AC-7 / AC-8 / AC-9 -- the bounded wait itself
# ---------------------------------------------------------------------------

def test_wait_timeout_bounds_the_wait_when_busy_skip_and_no_fallback(
        monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.delenv("PRISM_WORKER_FALLBACK_S", raising=False)
    assert tr._wait_timeout_s(900, None, True) == tr._ENGINE_BUSY_RETRY_S


def test_wait_timeout_never_lengthens_an_explicit_shorter_fallback(
        monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.setenv("PRISM_WORKER_FALLBACK_S", "2")
    assert tr._wait_timeout_s(900, None, True) == 2.0, (
        "the bounded retry is a CEILING, never a floor -- an operator's "
        "own shorter fallback must win")


def test_wait_timeout_is_untouched_when_nothing_was_deferred(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.delenv("PRISM_WORKER_FALLBACK_S", raising=False)
    assert tr._wait_timeout_s(900, None, False) is None, (
        "the reactive guarantee: a quiet tick with nothing eligible must "
        "still block indefinitely")


# ---------------------------------------------------------------------------
# AC-10 -- wired into the real loop
# ---------------------------------------------------------------------------

def test_loop_uses_the_bounded_retry_when_a_sweep_defers_for_a_busy_slot(
        monkeypatch):
    """Wires the flag through to the real _loop: a sweep that sets
    _last_engine_busy_skip must make the loop's OWN wait bounded, not
    just the pure _wait_timeout_s helper in isolation -- with no wake()
    call at all, only the bounded retry can bring the second sweep."""
    from prism_service.services import task_runner as tr
    from prism_service.services import wakeups

    sweeps = []
    swept = threading.Event()

    def _fake_sweep_once():
        tr._last_engine_busy_skip = True
        sweeps.append(time.monotonic())
        swept.set()
        return None

    monkeypatch.setattr(tr, "sweep_once", _fake_sweep_once)
    monkeypatch.setattr(tr, "_ENGINE_BUSY_RETRY_S", 0.2)
    # Deterministic regardless of how long this process has been up --
    # unlike the pre-existing (known flaky on a fresh process)
    # test_wake_cuts_the_wait_short_instead_of_sitting_out_the_interval,
    # this test must not depend on PRISM_WORKER_WARMUP_S having already
    # elapsed since process start.
    monkeypatch.setattr(wakeups, "wait_out_startup_warmup", lambda: None)
    tr._wake_event.clear()

    stop_event = threading.Event()
    t = threading.Thread(target=tr._loop, args=(999, stop_event), daemon=True)
    try:
        t.start()
        assert swept.wait(timeout=2), "first sweep never ran"
        swept.clear()

        assert swept.wait(timeout=2), (
            "a sweep that deferred for a busy engine slot must retry "
            "boundedly instead of sitting out the full interval")
        assert sweeps[1] - sweeps[0] < 2
    finally:
        stop_event.set()
        tr.wake()  # cut any remaining wait short so the thread notices
        t.join(timeout=2)
