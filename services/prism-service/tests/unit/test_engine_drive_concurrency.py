"""The engine has ONE parallel slot; the daemon must never open more than
PRISM_DRIVE_CONCURRENCY real dispatches at a time (task 8ddbba7f,
2026-09-13).

Live incident (owner's own screenshot, 2026-09-13 00:20Z): the Workflows
canvas showed ~6 tasks "in progress" with running step agents, while this
instance's local inference engine serves exactly ONE request at a time
(services/local-inference/launch.sh `--parallel 1`). The daemon had 5 tasks
in_progress and several `claude -p` step agents alive at once, load 15, and
the daemon's own API routes hung. Two independent dispatchers -- task_runner
(a periodic sweep, PRISM_TASK_RUNNER_CONCURRENCY default 4, threaded) and
dispatch.py's `_drive_now` instant handoff (fired on every step advance,
with NO concurrency check at all) -- both spawn real `claude_cli.invoke()`
subprocesses with nothing capping how many run at once.

This pins the fix: `dispatch_guard.py`'s existing chokepoint
(`try_begin`/`end_dispatch`, already the one place every real dispatch from
every seat passes through) now also enforces PRISM_DRIVE_CONCURRENCY
(default 1), counted from this process's own open dispatch tickets (a real
step-agent process it spawned) unioned with any live `drive_heartbeat` row
from one of the two autonomous seats -- never from a task's `status`
column. `eligible_tasks`, `dispatch._drive_now`, and
`resume_actuator.dispatch_once` each consult the same read-only
`engine_slot_reason` as an advisory pre-check so a busy slot costs nothing
before the real, atomic reservation in `try_begin`.

  AC-1 -- default concurrency is 1.
  AC-2 -- with one ticket open, a second try_begin for a DIFFERENT task is
          refused with a reason naming "engine slot busy", and the second
          task's history carries no DISPATCH_ACTION row for it.
  AC-3 -- ending the first ticket frees the slot for the second.
  AC-4 -- PRISM_DRIVE_CONCURRENCY=2 lets two tickets be open at once.
  AC-5 -- task_runner.eligible_tasks returns [] while the slot is busy.
  AC-6 -- dispatch._drive_now refuses (no thread spawned) and reports the
          reason, without ever calling task_runner._run_one_step.
  AC-7 -- resume_actuator.dispatch_once defers (not a failed attempt, no
          retry budget spent) while the slot is busy.
  AC-8 -- conductor_service.activity_for reports "queued", never "stalled",
          for an in_progress agent-step task with the slot held elsewhere.
"""

from __future__ import annotations

import uuid

import pytest


def _project() -> str:
    return "drive-concurrency-" + uuid.uuid4().hex[:8]


@pytest.fixture()
def make_task():
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(project, **kwargs):
        ctx = get_project(project)
        task = ctx.task_svc.create(
            title=kwargs.pop("title", "drive concurrency task"), **kwargs)
        ctx.task_svc.update(task.id, status="in_progress",
                            workflow_step="implement_tasks")
        created.append(task.id)
        return ctx, ctx.task_svc.get(task.id)

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    from prism_service.services import dispatch_guard as dg

    monkeypatch.delenv("PRISM_DRIVE_CONCURRENCY", raising=False)
    dg.reset_drive_concurrency_cache()
    yield
    dg.reset_drive_concurrency_cache()


def test_default_drive_concurrency_is_one():
    from prism_service.services import dispatch_guard as dg

    assert dg._drive_concurrency() == 1


def test_second_task_refused_while_the_one_slot_is_held(make_task):
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task_a = make_task(project)
    ctx2, task_b = make_task(project)

    ticket_a, reason_a = dg.try_begin(project, task_a.id, "implement_tasks",
                                      "task-runner")
    assert ticket_a is not None, reason_a

    ticket_b, reason_b = dg.try_begin(project, task_b.id, "implement_tasks",
                                      "task-runner")
    assert ticket_b is None
    assert "engine slot busy" in (reason_b or ""), reason_b
    assert task_a.id[:8] in (reason_b or "") or "implement_tasks" in (reason_b or "")

    dispatched_b = [r for r in ctx2.task_svc.history(task_b.id)
                    if r.action == dg.DISPATCH_ACTION]
    assert dispatched_b == [], (
        "a refused dispatch must never write a DISPATCH_ACTION row")
    refused_b = [r for r in ctx2.task_svc.history(task_b.id)
                if r.action == dg.REFUSED_ACTION]
    assert len(refused_b) == 1, refused_b

    dg.end_dispatch(ticket_a)


def test_ending_the_first_ticket_frees_the_slot(make_task):
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task_a = make_task(project)
    ctx2, task_b = make_task(project)

    ticket_a, _ = dg.try_begin(project, task_a.id, "implement_tasks", "seat")
    assert ticket_a is not None
    dg.end_dispatch(ticket_a)

    ticket_b, reason_b = dg.try_begin(project, task_b.id, "implement_tasks",
                                      "seat")
    assert ticket_b is not None, reason_b
    dg.end_dispatch(ticket_b)


def test_concurrency_two_allows_two_open_tickets(make_task, monkeypatch):
    from prism_service.services import dispatch_guard as dg

    monkeypatch.setenv("PRISM_DRIVE_CONCURRENCY", "2")
    dg.reset_drive_concurrency_cache()

    project = _project()
    ctx, task_a = make_task(project)
    ctx2, task_b = make_task(project)

    ticket_a, reason_a = dg.try_begin(project, task_a.id, "implement_tasks",
                                      "seat")
    ticket_b, reason_b = dg.try_begin(project, task_b.id, "implement_tasks",
                                      "seat")
    assert ticket_a is not None, reason_a
    assert ticket_b is not None, reason_b

    dg.end_dispatch(ticket_a)
    dg.end_dispatch(ticket_b)


def test_eligible_tasks_skips_a_candidate_while_a_different_task_holds_the_slot(
        monkeypatch):
    """A candidate task must be excluded from its OWN engine-slot check
    (exclude_task_id=t.id) -- otherwise a task's own fresh beat would read
    as 'the slot is busy' and disqualify itself, the exact regression this
    caught against test_rest_drive_is_visible_on_the_board.py's AC-5."""
    from prism_service.services import task_runner as tr

    monkeypatch.setattr(tr, "_spend_ceiling_crossed", lambda: False)
    monkeypatch.setattr(tr, "_system_overloaded", lambda: False)
    monkeypatch.setattr(tr, "_engine_unreachable", lambda: False)
    monkeypatch.setattr(tr, "_foreign_driver_on", lambda p, tid: "")

    from prism_service.services import dispatch_guard as dg

    # "occupant" holds the slot; a check excluding "occupant" itself sees
    # it free, a check excluding anything else sees it busy.
    monkeypatch.setattr(
        dg, "engine_slot_reason",
        lambda project, exclude_task_id="": (
            None if exclude_task_id == "occupant"
            else "engine slot busy: occupant at implement_tasks"))

    class _T:
        def __init__(self, tid, step):
            self.id, self.workflow_step = tid, step

    class _Svc:
        def list(self, **_kw):
            return [_T("occupant", "implement_tasks"),
                    _T("someone-else", "implement_tasks")]

    import types
    monkeypatch.setattr(
        "prism_service.project_context.get_project",
        lambda p: types.SimpleNamespace(task_svc=_Svc()))

    assert tr.eligible_tasks("p", 5) == ["occupant"], (
        "the occupant's own re-check must not disqualify itself, and the "
        "other task must be excluded while the slot is busy")


def test_drive_now_refuses_without_spawning_a_thread(monkeypatch):
    from prism_service.services import dispatch, dispatch_guard as dg

    monkeypatch.setattr(dg, "engine_slot_reason",
                        lambda project, exclude_task_id="": (
                            "engine slot busy: abcd1234 at implement_tasks"))

    invoked = []

    def _tripwire(*a, **kw):
        invoked.append((a, kw))
        raise AssertionError(
            "task_runner._run_one_step must never be called for a task "
            "the engine has no slot for")

    from prism_service.services import task_runner
    monkeypatch.setattr(task_runner, "_run_one_step", _tripwire)

    reason = dispatch._drive_now("some-task", "p")

    assert reason == "engine slot busy: abcd1234 at implement_tasks"
    assert invoked == []


def test_after_step_reports_drove_false_with_reason_when_slot_busy(monkeypatch):
    from prism_service.services import dispatch

    class _T:
        status = "in_progress"
        workflow_step = "implement_tasks"

    # _task_svc_for is used twice: once by after_step to fetch the task,
    # once by _record_queued_once to write history. One fake with both
    # behaviours covers both call sites.
    history: list = []

    class _Svc:
        def get(self, task_id):
            return _T()

        def record_history(self, task_id, action, details, actor):
            history.append((task_id, action, details, actor))

    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: _Svc())
    monkeypatch.setattr(dispatch, "_is_agent_step", lambda step: True)
    monkeypatch.setattr(dispatch, "_foreign_driver_on",
                        lambda task_id, project: "")
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda task_id, project: "engine slot busy: xyz")

    result = dispatch.after_step("after-step-" + uuid.uuid4().hex[:8], "p")

    assert result["drove"] is False
    assert result["reason"] == "engine slot busy: xyz"
    assert any(a == "engine_slot_queued" for _, a, _, _ in history), history


def test_after_step_dedupes_the_queued_history_row(monkeypatch):
    from prism_service.services import dispatch

    history: list = []

    class _T:
        status = "in_progress"
        workflow_step = "implement_tasks"

    class _Svc:
        def get(self, task_id):
            return _T()

        def record_history(self, task_id, action, details, actor):
            history.append(action)

    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: _Svc())
    monkeypatch.setattr(dispatch, "_is_agent_step", lambda step: True)
    monkeypatch.setattr(dispatch, "_foreign_driver_on",
                        lambda task_id, project: "")
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda task_id, project: "engine slot busy: xyz")

    dispatch.after_step("dedupe-task", "p")
    dispatch.after_step("dedupe-task", "p")

    assert history.count("engine_slot_queued") == 1, history


def test_resume_actuator_defers_without_spending_the_retry_budget(
        make_task, monkeypatch):
    from prism_service.services import resume_actuator as ra
    from prism_service.services import dispatch_guard as dg

    project = _project()
    ctx, task_a = make_task(project)
    ctx2, task_b = make_task(project)

    # Task A genuinely holds the one slot.
    ticket_a, _ = dg.try_begin(project, task_a.id, "implement_tasks",
                               ra.SEAT)
    assert ticket_a is not None

    result = ra.dispatch_once(project, task_b.id)

    assert result.get("deferred") is True, result
    assert "engine slot busy" in (result.get("reason") or ""), result

    from prism_service.services import resume_attempts_data as rad
    scores_db = ra._scores_db_for(project)
    assert rad.attempt_count(scores_db, task_b.id) == 0, (
        "an engine-busy refusal must never spend the retry budget")

    dg.end_dispatch(ticket_a)


def test_activity_for_reports_queued_not_stalled_when_slot_busy_elsewhere(
        monkeypatch):
    from prism_service.services.conductor_service import ConductorService

    svc = ConductorService(":memory:", enable_engine=False)
    svc._project_name = "p"
    monkeypatch.setattr(svc, "_task_motion_s", lambda task: None)

    from prism_service.services import drive_heartbeat
    monkeypatch.setattr(drive_heartbeat, "latest", lambda db, tid: None)

    from prism_service.services import dispatch_guard as dg
    monkeypatch.setattr(
        dg, "engine_slot_reason",
        lambda project, exclude_task_id="": "engine slot busy: other1234")

    class _T:
        id = "this-task"
        status = "in_progress"
        workflow_step = "implement_tasks"
        gate_state = "none"

    monkeypatch.setattr(svc, "_children", lambda task: [])

    result = svc.activity_for(_T(), {"session_quiet_s": None})

    assert result["state"] == "queued", result
