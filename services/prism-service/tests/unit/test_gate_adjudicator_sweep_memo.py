"""The sweep must not re-fetch or re-adjudicate a project/task that has not
moved (task: adjmemo, 2026-09-13).

Measured live at 7.13.338 with ZERO in_progress tasks: gate_adjudicator.
sweep_once ran 136.8s, then 94s+, on EVERY cadence, over 26 tasks parked at
pending gates none of which changed -- the daemon sat at 157% CPU and every
API route hung while it ran. RED BY CONSTRUCTION at the base commit: before
this task, `sweep_once()` unconditionally re-fetched every project's gate
snapshot and re-ran the per-task backoff check on every pass, with no way
to skip a whole project when nothing about it had moved.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

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


def _make_tasks(n=26):
    return [FakeTask(f"t{i}") for i in range(n)]


def _fake_ctx(tasks):
    ctx = MagicMock()
    # sweep_once reads the lean gate_sweep_rows() snapshot (tick-cost pass,
    # 2026-09-13), never the project-wide list() -- see
    # test_gate_adjudicator_sweep_cost.py's own pin on that contract.
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


def test_second_sweep_with_nothing_changed_makes_zero_adjudicate_calls_and_is_fast(
        monkeypatch):
    tasks = _make_tasks(26)
    ctx, svc = _fake_ctx(tasks)
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

    first = ga.sweep_once()
    assert first == []
    assert ctx.task_svc.gate_sweep_rows.call_count == 1
    assert svc.adjudicate_green_gate.call_count == 26, (
        "first pass must judge every eligible task once")

    ctx.task_svc.gate_sweep_rows.reset_mock()
    svc.adjudicate_green_gate.reset_mock()

    started = time.monotonic()
    second = ga.sweep_once()
    elapsed_ms = (time.monotonic() - started) * 1000.0

    assert second == []
    assert ctx.task_svc.gate_sweep_rows.call_count == 0, (
        "nothing changed -- the project must not even be re-fetched")
    assert svc.adjudicate_green_gate.call_count == 0, (
        "nothing changed -- zero adjudicate_* calls on the second sweep")
    assert elapsed_ms < 100.0, f"second sweep took {elapsed_ms:.1f}ms"


def test_task_changed_signal_reevaluates_only_that_task(monkeypatch):
    tasks = _make_tasks(26)
    ctx, svc = _fake_ctx(tasks)
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

    ga.sweep_once()
    svc.adjudicate_green_gate.reset_mock()
    ctx.task_svc.gate_sweep_rows.reset_mock()

    # Simulate a real mutation: task t5's row actually changed AND the
    # write path signalled it -- exactly what task_service.update() does
    # on every real write.
    tasks[5].updated_at = "t1-changed"
    wakeups.signal("task_changed", "proj1", task_id="t5")

    ga.sweep_once()

    assert ctx.task_svc.gate_sweep_rows.call_count == 1, (
        "a real signal must trigger exactly one re-fetch of the project"
    )
    assert svc.adjudicate_green_gate.call_count == 1, (
        "only the ONE task whose key actually changed is re-adjudicated")
    (called_tid,), _ = svc.adjudicate_green_gate.call_args
    assert called_tid == "t5", called_tid


def test_no_signal_never_forces_a_rescan_by_default(monkeypatch):
    """Supersedes test_a_safety_net_forces_a_rescan_even_with_no_signal
    (removed 2026-09-13, owner: "that 15 min thing is dumb, it's all
    reactive and real time") -- the old default 300s wall-clock safety
    net is gone. With PRISM_WORKER_FALLBACK_S unset (the default), a
    project with no task_changed/shipped signal must stay skipped no
    matter how long it has been, even a simulated day."""
    monkeypatch.delenv("PRISM_WORKER_FALLBACK_S", raising=False)
    tasks = _make_tasks(3)
    ctx, svc = _fake_ctx(tasks)
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

    ga.sweep_once()
    ctx.task_svc.gate_sweep_rows.reset_mock()

    ga._LAST_PROJECT_SCAN["proj1"] = time.time() - 86400.0

    ga.sweep_once()

    assert ctx.task_svc.gate_sweep_rows.call_count == 0, (
        "age alone must never force a rescan when PRISM_WORKER_FALLBACK_S "
        "is unset"
    )


def test_prism_worker_fallback_s_opts_a_project_into_a_periodic_rescan(
    monkeypatch,
):
    """The explicit opt-in still works for an environment that genuinely
    needs it (a write path that bypasses task_service.update()/
    ship_worker's own signal calls)."""
    monkeypatch.setenv("PRISM_WORKER_FALLBACK_S", "5")
    tasks = _make_tasks(3)
    ctx, svc = _fake_ctx(tasks)
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

    ga.sweep_once()
    ctx.task_svc.gate_sweep_rows.reset_mock()

    ga._LAST_PROJECT_SCAN["proj1"] = time.time() - 6.0

    ga.sweep_once()

    assert ctx.task_svc.gate_sweep_rows.call_count == 1, (
        "an explicit PRISM_WORKER_FALLBACK_S must still force a rescan "
        "once it elapses"
    )
