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
