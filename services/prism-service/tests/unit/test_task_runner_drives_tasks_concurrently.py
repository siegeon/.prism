"""The drive worker advances more than one task per sweep.

THE CEILING THIS LIFTS. sweep_once() drove "the first eligible task found
and stop -- AT MOST one task advances per tick", globally, across every
project. That is a concurrency of exactly ONE for the only seat that can
move the board on its own. implement_tasks alone has a ~474s median, and a
task needs ~10 steps, so a backlog of ~90 open tasks could never finish in
any useful time however many gates were cleared or stalls fixed. Owner,
2026-09-05, looking at the board: "i still see 89 open tasks".

Concurrency is bounded and the existing safety rails still decide: the host
load breaker (_system_overloaded) and the spend ceiling
(_spend_ceiling_crossed) are consulted exactly as before and refuse the
whole tick, and run_one_step's own _claim/_release lease still makes it
impossible to drive one task twice at once.
"""
from __future__ import annotations

import threading

import pytest


@pytest.fixture
def tr(monkeypatch):
    from prism_service.services import task_runner as t

    monkeypatch.setattr(t, "_system_overloaded", lambda: False)
    monkeypatch.setattr(t, "_spend_ceiling_crossed", lambda: False)
    monkeypatch.setattr(t, "_rr_index", 0)
    return t


def _wire(tr, monkeypatch, per_project, record):
    """One project 'p' whose eligible set is `per_project`, and a
    run_one_step that records what it was asked to drive."""
    from prism_service import project_context

    monkeypatch.setattr(project_context, "get_all_projects", lambda: ["p"])
    monkeypatch.setattr(
        tr, "eligible_tasks",
        lambda project, limit: list(per_project)[:limit], raising=False)

    def _run(pid, tid):
        record.append((pid, tid))
        return {"ok": True, "task_id": tid}

    monkeypatch.setattr(tr, "run_one_step", _run)


def test_the_default_concurrency_is_more_than_one(tr, monkeypatch):
    """AC-1. The whole point: with nothing configured, the seat drives more
    than one task per sweep. A default of 1 would leave the ceiling exactly
    where it was."""
    monkeypatch.delenv("PRISM_TASK_RUNNER_CONCURRENCY", raising=False)
    assert tr._concurrency() > 1


def test_a_sweep_drives_several_eligible_tasks_in_one_pass(tr, monkeypatch):
    """AC-2. Three eligible tasks, room for three: all three advance in the
    SAME sweep, where the old loop drove one and returned."""
    monkeypatch.setenv("PRISM_TASK_RUNNER_CONCURRENCY", "3")
    seen: list = []
    _wire(tr, monkeypatch, ["t1", "t2", "t3"], seen)

    tr.sweep_once()

    assert sorted(t for _p, t in seen) == ["t1", "t2", "t3"], seen


def test_concurrency_is_bounded_by_its_setting(tr, monkeypatch):
    """AC-3. Five eligible, room for two: exactly two run. The bound is real,
    so a huge backlog cannot fan out unboundedly onto the host."""
    monkeypatch.setenv("PRISM_TASK_RUNNER_CONCURRENCY", "2")
    seen: list = []
    _wire(tr, monkeypatch, ["t1", "t2", "t3", "t4", "t5"], seen)

    tr.sweep_once()

    assert len(seen) == 2, seen


def test_the_tasks_really_do_overlap_in_time(tr, monkeypatch):
    """AC-4. Concurrency means CONCURRENT -- not a faster serial loop. Each
    drive blocks until all of them have started, which only completes if
    they genuinely run at the same time."""
    monkeypatch.setenv("PRISM_TASK_RUNNER_CONCURRENCY", "3")
    from prism_service import project_context

    monkeypatch.setattr(project_context, "get_all_projects", lambda: ["p"])
    monkeypatch.setattr(
        tr, "eligible_tasks",
        lambda project, limit: ["t1", "t2", "t3"][:limit], raising=False)

    barrier = threading.Barrier(3, timeout=10)
    overlapped = []

    def _run(pid, tid):
        barrier.wait()          # TimeoutError unless all three are in here
        overlapped.append(tid)
        return {"ok": True, "task_id": tid}

    monkeypatch.setattr(tr, "run_one_step", _run)

    tr.sweep_once()

    assert sorted(overlapped) == ["t1", "t2", "t3"], overlapped


def test_an_overloaded_host_still_refuses_the_whole_tick(tr, monkeypatch):
    """AC-5. The safety rail outranks the new throughput: no drive at all,
    whatever the concurrency setting says."""
    monkeypatch.setenv("PRISM_TASK_RUNNER_CONCURRENCY", "8")
    monkeypatch.setattr(tr, "_system_overloaded", lambda: True)
    seen: list = []
    _wire(tr, monkeypatch, ["t1", "t2", "t3"], seen)

    assert tr.sweep_once() is None
    assert seen == []


def test_a_spend_ceiling_still_refuses_the_whole_tick(tr, monkeypatch):
    """AC-6. Same for the cost breaker."""
    monkeypatch.setenv("PRISM_TASK_RUNNER_CONCURRENCY", "8")
    monkeypatch.setattr(tr, "_spend_ceiling_crossed", lambda: True)
    seen: list = []
    _wire(tr, monkeypatch, ["t1", "t2", "t3"], seen)

    assert tr.sweep_once() is None
    assert seen == []


def test_a_single_eligible_task_still_returns_its_result(tr, monkeypatch):
    """AC-7. The existing contract: one eligible task still returns that
    run's own result dict, so the load-breaker suite's
    `result["ok"] is True` keeps holding."""
    monkeypatch.setenv("PRISM_TASK_RUNNER_CONCURRENCY", "4")
    seen: list = []
    _wire(tr, monkeypatch, ["only"], seen)

    res = tr.sweep_once()

    assert res is not None and res["ok"] is True, res
    assert seen == [("p", "only")]


def test_nothing_eligible_still_returns_none(tr, monkeypatch):
    """AC-8. An idle board is not an error."""
    monkeypatch.setenv("PRISM_TASK_RUNNER_CONCURRENCY", "4")
    seen: list = []
    _wire(tr, monkeypatch, [], seen)

    assert tr.sweep_once() is None
    assert seen == []


def test_eligible_tasks_agrees_with_eligible_task_on_the_first_id(monkeypatch):
    """AC-9. The plural reader is the SAME rule as the singular one, not a
    second copy that can drift -- limit=1 must give exactly what
    eligible_task gives, including its gate/in-flight/foreign-driver skips."""
    from prism_service.services import task_runner as t

    monkeypatch.setattr(t, "_system_overloaded", lambda: False)
    monkeypatch.setattr(t, "_spend_ceiling_crossed", lambda: False)

    class _T:
        def __init__(self, tid, step):
            self.id, self.workflow_step = tid, step

    class _Svc:
        def list(self, **_kw):
            # first is parked at a GATE and must be skipped by both readers
            return [_T("gated", "green_gate"), _T("agent-1", "implement_tasks"),
                    _T("agent-2", "implement_tasks")]

    import types
    monkeypatch.setattr(
        "prism_service.project_context.get_project",
        lambda p: types.SimpleNamespace(task_svc=_Svc()))
    monkeypatch.setattr(t, "_foreign_driver_on", lambda p, tid: "")

    assert t.eligible_tasks("p", 1) == [t.eligible_task("p")]
    assert t.eligible_tasks("p", 5) == ["agent-1", "agent-2"]
