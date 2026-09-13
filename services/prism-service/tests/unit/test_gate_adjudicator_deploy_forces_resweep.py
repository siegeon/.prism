"""A `deployed` signal forces one full re-sweep, bypassing both the
project-level scan skip and the per-task backoff (task a65c66e5,
2026-09-13, ops incident).

Live: the certainty seat's own self-heal (design_packet.py) landed and the
daemon was deployed, but task a65c66e5 itself never re-evaluated -- nothing
about ITS ROW changed (same updated_at, same gate_state, same
workflow_step), only the CODE that reads it did. The project-level skip
(`_project_needs_scan`) and the per-task backoff (`_backoff_should_skip`)
are both keyed on the row being unchanged, so neither memo would ever
notice a landing on its own -- the seat only wakes reactively on
task_changed/shipped, and a deploy raises neither.

Fix: `sweep_once(force=True)` -- wired from `_loop` whenever a `deployed`
signal landed since the last wait() -- bypasses both memos for that one
pass, so a park whose CAUSE a landing just fixed gets a fresh look.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import gate_adjudicator as ga  # noqa: E402
from prism_service.services import gate_adjudicator_memo as gam  # noqa: E402
from prism_service.services import wakeups  # noqa: E402


class FakeTask:
    def __init__(self, tid, updated_at="t0", gate_state="pending",
                workflow_step="green_gate"):
        self.id = tid
        self.updated_at = updated_at
        self.gate_state = gate_state
        self.workflow_step = workflow_step
        self.gate_reason = ""


def _make_tasks(n=5):
    return [FakeTask(f"t{i}") for i in range(n)]


def _fake_ctx(tasks):
    ctx = MagicMock()
    ctx.task_svc.gate_sweep_rows = MagicMock(return_value=tasks)
    by_id = {t.id: t for t in tasks}
    ctx.task_svc.get = MagicMock(side_effect=lambda tid: by_id.get(tid))
    svc = ctx.conductor_svc
    svc.adjudicate_green_gate = MagicMock(return_value={"ok": False})
    svc._oracle_receipt_refusal = MagicMock(return_value=("", None))
    return ctx, svc


def setup_function(_):
    ga._BACKOFF.clear()
    ga._LAST_PROJECT_SCAN.clear()
    ga._LAST_PROJECT_ELIGIBLE.clear()
    ga._LAST_PROJECT_CURSOR.clear()
    gam.reset_for_tests()
    wakeups._reset_for_tests()


def _wire(monkeypatch, ctx):
    monkeypatch.setattr(
        "prism_service.project_context.get_all_projects",
        lambda: ["proj1"])
    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda pid: ctx)
    monkeypatch.setattr(
        "prism_service.services.green_rewind.maybe_rewind",
        lambda *a, **k: None)
    monkeypatch.setattr(
        "prism_service.services.gate_agent.adjudicate",
        lambda *a, **k: None)
    monkeypatch.setattr(gam, "workspace_head_sha", lambda tid: "")


def test_a_second_unforced_sweep_skips_the_memoized_project(monkeypatch):
    """Baseline: without force, a second sweep with nothing changed makes
    zero adjudicate calls -- the memo this fix must still respect."""
    tasks = _make_tasks()
    ctx, svc = _fake_ctx(tasks)
    _wire(monkeypatch, ctx)

    ga.sweep_once()
    svc.adjudicate_green_gate.reset_mock()
    ctx.task_svc.gate_sweep_rows.reset_mock()

    ga.sweep_once()

    assert ctx.task_svc.gate_sweep_rows.call_count == 0
    assert svc.adjudicate_green_gate.call_count == 0


def test_force_bypasses_the_project_and_per_task_memo(monkeypatch):
    tasks = _make_tasks()
    ctx, svc = _fake_ctx(tasks)
    _wire(monkeypatch, ctx)

    first = ga.sweep_once()
    assert first == []
    assert svc.adjudicate_green_gate.call_count == len(tasks)

    svc.adjudicate_green_gate.reset_mock()
    ctx.task_svc.gate_sweep_rows.reset_mock()

    # No task_changed/shipped signal at all -- a normal pass would skip
    # every project (proven above). force=True must re-adjudicate anyway.
    second = ga.sweep_once(force=True)

    assert second == []
    assert ctx.task_svc.gate_sweep_rows.call_count == 1, (
        "force must re-fetch the project even though nothing signalled")
    assert svc.adjudicate_green_gate.call_count == len(tasks), (
        "force must re-adjudicate every eligible task, bypassing both the "
        "project-level skip and the per-task backoff")


def test_a_deployed_signal_is_visible_to_changed_since_after_a_baseline():
    """The exact primitive `_loop` uses to decide `force` -- pins that a
    `deployed` signal (main.py's boot signal / deploy_worker's confirm)
    shows up via wakeups.changed_since against a baseline taken before it,
    and not against one taken after."""
    baseline = time.time()
    time.sleep(0.01)
    wakeups.signal("deployed", "*")

    assert wakeups.changed_since(["deployed"], None, baseline), (
        "a deployed signal after the baseline must be visible")

    later_baseline = time.time()
    assert not wakeups.changed_since(["deployed"], None, later_baseline), (
        "a baseline taken AFTER the signal must not see it again"
    )


# ---------------------------------------------------------------------------
# Second round, task a65c66e5 (2026-09-13): a `deployed` signal fired at API
# startup is invisible to a wait baseline taken by THIS loop after warmup --
# the worker-host process (and this thread) does not exist yet when main.py
# signals it. A fresh worker host must therefore force its FIRST pass
# unconditionally, never relying on catching that boot signal.
# ---------------------------------------------------------------------------


class _StopLoop(Exception):
    """Sentinel to escape `_loop`'s `while True` after N iterations."""


def test_the_first_pass_after_warmup_is_unconditional_with_no_signals(
        monkeypatch):
    from prism_service.services import gate_adjudicator as ga
    from prism_service.services import wakeups as wk

    wk._reset_for_tests()
    forces: list[bool] = []

    def fake_sweep_once(force=False, force_backoff=None):
        forces.append(force)
        # Nothing deferred -- a single-gate (or otherwise budget-fitting)
        # boot drains in exactly one pass.
        ga._last_eligible_count = 1
        ga._last_changed_count = 1
        return []

    wait_calls = {"n": 0}

    def fake_wait(kinds, timeout=None):
        wait_calls["n"] += 1
        if wait_calls["n"] >= 2:
            raise _StopLoop()
        return True

    monkeypatch.setattr(ga, "sweep_once", fake_sweep_once)
    monkeypatch.setattr(wk, "wait_out_startup_warmup", lambda: None)
    monkeypatch.setattr(wk, "lower_thread_priority", lambda: None)
    monkeypatch.setattr(wk, "wait", fake_wait)
    monkeypatch.setattr(wk, "worker_fallback_s", lambda: None)

    with pytest.raises(_StopLoop):
        ga._loop(60)

    assert forces == [True, False], (
        f"pass 1 (post-warmup, no signals at all) must force; pass 2 (no "
        f"new signal since) must not -- got {forces}")


def test_a_backlog_bigger_than_one_budget_drains_across_several_passes(
        monkeypatch):
    """Live, task a65c66e5 third round: 31 real pending gates, only ~10
    fit in one _SWEEP_BUDGET_S window. The first pass bypasses per-task
    backoff too (nothing has an entry yet); every CONTINUATION drain pass
    bypasses only the project scan, letting real backoff gate whatever
    was already refused this boot so the remaining budget goes to
    never-yet-touched tasks -- and the loop goes reactive the instant a
    pass reports nothing left to force."""
    from prism_service.services import gate_adjudicator as ga
    from prism_service.services import wakeups as wk

    wk._reset_for_tests()
    calls: list[tuple] = []
    remaining = {"n": 10}
    per_pass = [3, 3, 4]  # drains fully on the third forced pass

    def fake_sweep_once(force=False, force_backoff=None):
        calls.append((force, force_backoff))
        done = per_pass[len(calls) - 1] if len(calls) <= len(per_pass) else 0
        ga._last_eligible_count = remaining["n"]
        ga._last_changed_count = done
        remaining["n"] -= done
        return []

    wait_calls = {"n": 0}

    def fake_wait(kinds, timeout=None):
        wait_calls["n"] += 1
        if wait_calls["n"] >= 2:
            raise _StopLoop()
        return True

    monkeypatch.setattr(ga, "sweep_once", fake_sweep_once)
    monkeypatch.setattr(wk, "wait_out_startup_warmup", lambda: None)
    monkeypatch.setattr(wk, "lower_thread_priority", lambda: None)
    monkeypatch.setattr(wk, "wait", fake_wait)
    monkeypatch.setattr(wk, "worker_fallback_s", lambda: None)

    with pytest.raises(_StopLoop):
        ga._loop(60)

    assert calls == [
        (True, True),    # pass 1: force scan + force backoff (nothing yet)
        (True, False),   # pass 2: force scan only -- real backoff applies
        (True, False),   # pass 3: force scan only -- fully drains here
        (False, False),  # reactive pass: no new signal, no force at all
    ], calls


def test_a_stuck_drain_eventually_gives_up_and_goes_reactive(monkeypatch):
    """A pathological backlog that never reports fully drained must not
    hang startup forever -- the boot-drain ceiling caps it."""
    from prism_service.services import gate_adjudicator as ga
    from prism_service.services import wakeups as wk

    wk._reset_for_tests()
    calls: list[tuple] = []

    def fake_sweep_once(force=False, force_backoff=None):
        calls.append((force, force_backoff))
        # Never converges: always one more than changed.
        ga._last_eligible_count = 5
        ga._last_changed_count = 4
        return []

    def fake_wait(kinds, timeout=None):
        raise _StopLoop()

    monkeypatch.setattr(ga, "sweep_once", fake_sweep_once)
    monkeypatch.setattr(wk, "wait_out_startup_warmup", lambda: None)
    monkeypatch.setattr(wk, "lower_thread_priority", lambda: None)
    monkeypatch.setattr(wk, "wait", fake_wait)
    monkeypatch.setattr(wk, "worker_fallback_s", lambda: None)

    with pytest.raises(_StopLoop):
        ga._loop(60)

    assert len(calls) == ga._MAX_BOOT_DRAIN_PASSES, (
        f"a never-converging drain must stop at the ceiling and go "
        f"reactive, not hang forever -- got {len(calls)} passes")


# ---------------------------------------------------------------------------
# Fourth round, task a65c66e5 (2026-09-13): _SWEEP_BUDGET_S (2s) only ever
# gates STARTING the next task -- it never bounds one already running, and a
# real pass was observed taking ~20s live (one slow adjudication). The
# worker host is its own process since 7.13.341, so this no longer risks
# delaying a live API request, but a chain of several such passes can still
# burn real minutes -- so the drain also gets a WALL-CLOCK ceiling, and the
# slowest task(s) get surfaced instead of just the pass elapsed time.
# ---------------------------------------------------------------------------


def test_boot_drain_stops_at_the_wall_clock_ceiling(monkeypatch):
    from prism_service.services import gate_adjudicator as ga
    from prism_service.services import wakeups as wk

    wk._reset_for_tests()
    calls: list[tuple] = []

    def fake_sweep_once(force=False, force_backoff=None):
        calls.append((force, force_backoff))
        ga._last_eligible_count = 5
        ga._last_changed_count = 4  # never converges on its own
        return []

    def fake_wait(kinds, timeout=None):
        raise _StopLoop()

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 25.0  # 3rd read crosses the 60s ceiling
        return clock["t"]

    monkeypatch.setattr(ga, "sweep_once", fake_sweep_once)
    monkeypatch.setattr(ga.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(wk, "wait_out_startup_warmup", lambda: None)
    monkeypatch.setattr(wk, "lower_thread_priority", lambda: None)
    monkeypatch.setattr(wk, "wait", fake_wait)
    monkeypatch.setattr(wk, "worker_fallback_s", lambda: None)

    with pytest.raises(_StopLoop):
        ga._loop(60)

    assert 0 < len(calls) < ga._MAX_BOOT_DRAIN_PASSES, (
        f"the wall-clock ceiling must stop a never-converging drain well "
        f"before the pass-count ceiling would -- got {len(calls)} passes")


def test_top_3_slowest_tasks_are_surfaced(monkeypatch):
    """Live, task a65c66e5 fourth round: a pass taking far longer than
    _SWEEP_BUDGET_S implies is almost always one or two tasks' own
    adjudication running long -- surface WHICH ones instead of leaving a
    human to guess from the pass's total elapsed time alone."""
    from prism_service.services import gate_adjudicator as ga

    tasks = _make_tasks(5)
    ctx, svc = _fake_ctx(tasks)
    _wire(monkeypatch, ctx)
    delays = {"t0": 0.08, "t1": 0.0, "t2": 0.05, "t3": 0.0, "t4": 0.02}

    def fake_adjudicate(tid):
        time.sleep(delays.get(tid, 0.0))
        return {"ok": False}

    svc.adjudicate_green_gate = MagicMock(side_effect=fake_adjudicate)

    ga.sweep_once(force=True)

    slow_ids = [t for t, _ in ga._last_top_slow]
    assert slow_ids == ["t0", "t2", "t4"], ga._last_top_slow
    assert len(ga._last_top_slow) == 3


# ---------------------------------------------------------------------------
# Fourth round continued: WHY task a65c66e5 was never reached at all -- two
# real, distinct bugs found live from the instrumentation above.
#
# (1) green_rewind.maybe_rewind's "inconclusive" (manual-evidence-required)
#     shape is truthy but represents NO decision and NO state change; the
#     caller treated any truthy return as terminal and skipped the normal
#     backoff write, so an inconclusive green_gate was retried on EVERY
#     pass forever -- live, tasks 09dd9464/7a72ebcb ate an ever-larger
#     share of the budget this way.
# (2) sweep_once always walked a project's gate-eligible tasks starting
#     from the same position -- fine until (1) let two tasks permanently
#     consume most of the budget every pass, at which point nothing behind
#     them (a65c66e5 included) was EVER reached, across 10 real passes.
# ---------------------------------------------------------------------------


def test_an_inconclusive_green_rewind_still_gets_a_backoff_entry(monkeypatch):
    from prism_service.services import gate_adjudicator as ga

    tasks = _make_tasks(1)
    ctx, svc = _fake_ctx(tasks)
    _wire(monkeypatch, ctx)
    svc.adjudicate_green_gate = MagicMock(return_value={"ok": False})
    monkeypatch.setattr(
        "prism_service.services.green_rewind.maybe_rewind",
        lambda *a, **k: {"ok": False, "inconclusive": True,
                        "task_id": tasks[0].id, "status": "manual",
                        "reason": "manual evidence required"})

    ga.sweep_once(force=True)

    assert tasks[0].id in ga._BACKOFF, (
        "an inconclusive (manual-evidence) green_gate must still get a "
        "backoff entry -- without one it is retried on every single pass "
        "forever, live: tasks 09dd9464/7a72ebcb")


def test_a_real_rewind_or_park_still_skips_the_backoff_write(monkeypatch):
    """The fix must not over-correct: an ACTUAL rewind (ok=True) or a
    spent rewind budget (parked=True) is a genuine terminal outcome for
    this pass and must keep skipping the backoff write, exactly as
    before."""
    from prism_service.services import gate_adjudicator as ga

    for shape in ({"ok": True, "task_id": "t0", "to_step": "implement_tasks"},
                 {"ok": False, "parked": True, "task_id": "t0"}):
        ga._BACKOFF.clear()
        tasks = _make_tasks(1)
        ctx, svc = _fake_ctx(tasks)
        _wire(monkeypatch, ctx)
        monkeypatch.setattr(
            "prism_service.services.green_rewind.maybe_rewind",
            lambda *a, **k: shape)
        svc.adjudicate_green_gate = MagicMock(return_value={"ok": False})
        ga.sweep_once(force=True)
        assert tasks[0].id not in ga._BACKOFF, (
            f"a real terminal outcome {shape} must still skip the backoff "
            f"write (the task moved or parked, not merely re-stamped)")


def test_rotate_from_cursor_resumes_right_after_it():
    from prism_service.services import gate_adjudicator as ga

    tasks = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
    out = ga._rotate_from_cursor(tasks, "b")
    assert [t["id"] for t in out] == ["c", "d", "a", "b"]


def test_rotate_from_cursor_wraps_when_the_cursor_was_last():
    from prism_service.services import gate_adjudicator as ga

    tasks = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    out = ga._rotate_from_cursor(tasks, "c")
    assert [t["id"] for t in out] == ["a", "b", "c"]


def test_rotate_from_cursor_when_the_cursor_task_has_left_the_set():
    """The cursor task itself may have been approved/rewound off the
    pending-gate list entirely between passes -- resume at the first
    remaining id that sorts after it, never restart at the front."""
    from prism_service.services import gate_adjudicator as ga

    tasks = [{"id": "a"}, {"id": "c"}, {"id": "d"}]
    out = ga._rotate_from_cursor(tasks, "b")
    assert [t["id"] for t in out] == ["c", "d", "a"]


def test_rotate_from_cursor_with_no_cursor_yet_is_just_sorted():
    from prism_service.services import gate_adjudicator as ga

    tasks = [{"id": "c"}, {"id": "a"}, {"id": "b"}]
    out = ga._rotate_from_cursor(tasks, "")
    assert [t["id"] for t in out] == ["a", "b", "c"]


def test_a_slow_prefix_no_longer_starves_the_rest_of_the_backlog(
        monkeypatch):
    """Integration shape of the live bug: each real adjudication costs
    enough that only ONE task fits in a pass's budget -- fairness must
    still let every task get a real look within a bounded number of
    passes, not restart at the front forever."""
    from prism_service.services import gate_adjudicator as ga

    tasks = _make_tasks(6)
    ctx, svc = _fake_ctx(tasks)
    _wire(monkeypatch, ctx)
    monkeypatch.setattr(ga, "_SWEEP_BUDGET_S", 0.05)

    def fake_adjudicate(tid):
        time.sleep(0.03)  # ~1 task fits per 0.05s budget window
        return {"ok": False}

    svc.adjudicate_green_gate = MagicMock(side_effect=fake_adjudicate)

    for _ in range(6):
        ga.sweep_once(force=True, force_backoff=False)
    seen = set(ga._BACKOFF.keys())

    assert seen == {f"t{i}" for i in range(6)}, seen
