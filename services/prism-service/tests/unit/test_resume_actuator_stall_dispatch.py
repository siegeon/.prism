"""A stalled drive resumes without a human nudge (task 7a72ebcb).

`ConductorService.activity_for` already DETECTS the exact state the owner
complained about (task_motion_s stale, session_quiet_s stale/absent, no
drive_heartbeat) -- see tests/test_activity_state.py's own
`test_stalled_both_stale`. What was missing is the ACTUATOR: on that
detection, something dispatches a real driver for the task instead of
waiting for a human to notice in chat and relaunch `implement` by hand.

This pins the new `prism_service.services.resume_actuator` module (mirrors
`task_runner.py` / `gate_adjudicator.py`: env-gated, off by default) and its
companion `resume_attempts_data` (mirrors `drive_heartbeat.py`: sqlite,
scores_db-scoped, plain dicts) against the task's own plan_diagram:

  sweep -> eligible (in_progress, non-gate step, gate_state none, activity
  'stalled') -> budget check -> dispatch (heartbeat + distinguishable
  history row + task_runner's own flow_start/invoke/flow_report path) ->
  advanced => reset budget / no-advance => increment budget -> budget spent
  => park with an explicit reason, never dispatch again.

Covers:
  AC-1 -- eligibility reads task_motion_s/session_quiet_s/heartbeat exactly
          as activity_for does, and only when workflow_step is not a gate
          and gate_state == 'none'.
  AC-2 -- an eligible task gets a driver dispatched with a new, attributable
          history row -- no human action.
  AC-3 -- the dispatch path never decides/skips a gate; eligibility itself
          never returns a task parked at a gate step.
  AC-4 -- the tile-backing activity_for state moves off 'stalled' the
          moment dispatch records its heartbeat.
  AC-5 -- a retry budget: N dispatches, then an explicit parked reason and
          no further auto-dispatch.
  AC-6 -- a sibling task at a gate step, and a sibling task the owner
          blocked, are never dispatched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

SEAT = "prism-resume-actuator"


# ---------------------------------------------------------------------------
# Lightweight fixture (mirrors tests/test_activity_state.py) -- pure
# eligibility logic needs no project/workspace machinery.
# ---------------------------------------------------------------------------

def _cond(tmp_path):
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    return cond, task_svc, scores_db


def _old_transition(task_svc, task_id, seconds_ago, to="write_failing_tests"):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    task_svc._db.execute(
        "INSERT INTO task_history (task_id, actor, action, details, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, "", "advance_task", f"to={to}", ts),
    )
    task_svc._db.commit()


def _stalled_task(task_svc, step="write_failing_tests"):
    """A task mid-workflow whose motion, session and heartbeat are ALL
    stale -- the exact activity_for('stalled') precondition."""
    t = task_svc.create(title="nothing driving it")
    task_svc.update(t.id, status="in_progress", workflow_step=step)
    _old_transition(task_svc, t.id, seconds_ago=600, to=step)
    return t


# ---------------------------------------------------------------------------
# AC-1 -- eligibility
# ---------------------------------------------------------------------------

def test_stalled_non_gate_task_is_eligible(tmp_path):
    from prism_service.services import resume_actuator as ra

    cond, task_svc, _ = _cond(tmp_path)
    t = _stalled_task(task_svc)
    act = cond.activity_for(task_svc.get(t.id), {"session_quiet_s": None})
    assert act["state"] == "stalled", act

    assert ra.is_stalled_and_eligible(
        task_svc.get(t.id), cond, {"session_quiet_s": None}) is True


def test_working_task_is_not_eligible(tmp_path):
    """A task with a recent transition (activity_for == 'working') must
    never be dispatched -- it already has a live driver."""
    from prism_service.services import resume_actuator as ra

    cond, task_svc, _ = _cond(tmp_path)
    t = task_svc.create(title="being driven")
    task_svc.update(t.id, status="in_progress", workflow_step="implement_tasks")
    task_svc.record_history(t.id, action="advance_task", details="to=implement_tasks")

    assert cond.activity_for(task_svc.get(t.id), {"session_quiet_s": None})["state"] == "working"
    assert ra.is_stalled_and_eligible(
        task_svc.get(t.id), cond, {"session_quiet_s": None}) is False


def test_adrift_task_is_not_eligible(tmp_path):
    """A live linked session (adrift, not stalled) means someone IS
    driving -- dispatching on top of them would double-drive the task."""
    from prism_service.services import resume_actuator as ra

    cond, task_svc, _ = _cond(tmp_path)
    t = _stalled_task(task_svc)
    phase = {"session_quiet_s": 30}  # <=90s: alive
    assert cond.activity_for(task_svc.get(t.id), phase)["state"] == "adrift"
    assert ra.is_stalled_and_eligible(task_svc.get(t.id), cond, phase) is False


def test_gate_step_task_is_not_eligible(tmp_path):
    """A task parked at a *_gate step is a WAIT for a distinct reviewer,
    never something this actuator may touch (AC-3's exclusion)."""
    from prism_service.services import resume_actuator as ra

    cond, task_svc, _ = _cond(tmp_path)
    t = task_svc.create(title="at a gate")
    task_svc.update(t.id, status="in_progress", workflow_step="green_gate",
                    gate_state="pending")
    _old_transition(task_svc, t.id, seconds_ago=600, to="green_gate")

    assert ra.is_stalled_and_eligible(
        task_svc.get(t.id), cond, {"session_quiet_s": None}) is False


# ---------------------------------------------------------------------------
# AC-6 -- blocked / gate siblings never dispatched, only the stalled one is
# ---------------------------------------------------------------------------

def _project():
    return "resume-" + uuid.uuid4().hex[:8]


@pytest.fixture()
def make_task():
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(project, **kwargs):
        ctx = get_project(project)
        task = ctx.task_svc.create(title=kwargs.pop("title", "resume task"), **kwargs)
        created.append(task.id)
        return ctx, task

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


def _backdate(ctx, task_id, seconds_ago, to):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    ctx.task_svc._db.execute(
        "INSERT INTO task_history (task_id, actor, action, details, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, "", "advance_task", f"to={to}", ts),
    )
    ctx.task_svc._db.commit()


def test_only_the_stalled_task_is_picked_over_gate_and_blocked_siblings(make_task):
    from prism_service.services import resume_actuator as ra

    project = _project()
    ctx, stalled = make_task(project)
    ctx.task_svc.update(stalled.id, status="in_progress",
                        workflow_step="write_failing_tests")
    _backdate(ctx, stalled.id, 600, "write_failing_tests")

    _, at_gate = make_task(project)
    ctx.task_svc.update(at_gate.id, status="in_progress",
                        workflow_step="green_gate", gate_state="pending")
    _backdate(ctx, at_gate.id, 600, "green_gate")

    _, paused = make_task(project)
    ctx.task_svc.update(paused.id, status="blocked",
                        workflow_step="implement_tasks")
    _backdate(ctx, paused.id, 600, "implement_tasks")

    picked = ra.eligible_task(project)
    assert picked == stalled.id, (
        f"expected only the genuinely stalled task {stalled.id} to be "
        f"picked; got {picked!r} (gate task {at_gate.id}, blocked task "
        f"{paused.id} must both be skipped)")


# ---------------------------------------------------------------------------
# AC-2 / AC-4 -- dispatch: attributable history row + activity flips off
# 'stalled' the moment dispatch records its heartbeat
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, text: str, exit_code: int = 0, run_id: str = "run-resume"):
        self._text = text
        self.exit_code = exit_code
        self.run_id = run_id
        self.usage = {}

    def final_text(self) -> str:
        return self._text


def test_dispatch_writes_a_distinguishable_history_row(make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.services import resume_actuator as ra

    project = _project()
    ctx, task = make_task(project)
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"), project=project)
    ctx.task_svc.update(task.id, status="in_progress")
    step = ctx.task_svc.get(task.id).workflow_step
    _backdate(ctx, task.id, 600, step)

    monkeypatch.setattr(claude_cli, "invoke",
                        lambda prompt, **kw: _FakeResult("", exit_code=1))

    res = ra.dispatch_once(project, task.id)
    assert res.get("ok") is not None, res

    rows = ctx.task_svc.history(task.id)
    dispatch_rows = [r for r in rows if r.action == ra.DISPATCH_ACTION]
    assert len(dispatch_rows) == 1, (
        f"dispatch must write exactly one {ra.DISPATCH_ACTION!r} row, "
        f"distinct from advance_task/gate_decide; got actions="
        f"{[r.action for r in rows]}")
    assert dispatch_rows[0].actor == SEAT, dispatch_rows[0]
    # Never folded into the transition/gate actions the tile's own
    # motion clock reads (conductor_service._task_motion_s).
    assert ra.DISPATCH_ACTION not in ("advance_task", "gate_decide")


def test_dispatch_flips_activity_off_stalled(make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.services import resume_actuator as ra

    project = _project()
    ctx, task = make_task(project)
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"), project=project)
    ctx.task_svc.update(task.id, status="in_progress")
    step = ctx.task_svc.get(task.id).workflow_step
    _backdate(ctx, task.id, 600, step)

    before = ctx.conductor_svc.activity_for(
        ctx.task_svc.get(task.id), ctx.conductor_svc.phase_progress(task.id))
    assert before["state"] == "stalled", before

    monkeypatch.setattr(claude_cli, "invoke",
                        lambda prompt, **kw: _FakeResult("", exit_code=1))
    ra.dispatch_once(project, task.id)

    after = ctx.conductor_svc.activity_for(
        ctx.task_svc.get(task.id), ctx.conductor_svc.phase_progress(task.id))
    assert after["state"] in ("driving", "working"), (
        f"the tile must move off 'stalled' the instant dispatch fires; "
        f"got {after!r}")


# ---------------------------------------------------------------------------
# AC-3 -- dispatch never decides a gate
# ---------------------------------------------------------------------------

def test_dispatch_path_never_calls_a_gate_outcome():
    """Structural pin (mirrors task_runner.py's own docstring guarantee and
    test_runner_usage_persisted.py's carry-path pin): the dispatch module
    must contain no approving-gate call of its own -- gate adjudication
    stays a distinct seat's job."""
    from pathlib import Path

    here = Path(__file__).resolve()
    service_root = here.parent.parent.parent
    src = (service_root / "prism_service/services/resume_actuator.py").read_text(
        encoding="utf-8")
    assert "gate_decide" not in src, (
        "resume_actuator.py must never call gate_decide -- gate decisions "
        "belong to a distinct-actor seat, never the dispatching actuator")


def test_eligible_task_never_returns_a_gate_step(make_task):
    from prism_service.services import resume_actuator as ra

    project = _project()
    ctx, task = make_task(project)
    ctx.task_svc.update(task.id, status="in_progress",
                        workflow_step="red_gate", gate_state="pending")
    _backdate(ctx, task.id, 600, "red_gate")

    assert ra.eligible_task(project) is None


# ---------------------------------------------------------------------------
# AC-5 -- retry budget
# ---------------------------------------------------------------------------

def test_retry_budget_parks_after_n_failed_dispatches(make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.services import resume_actuator as ra

    monkeypatch.setenv("PRISM_RESUME_ACTUATOR_MAX_RETRIES", "2")

    project = _project()
    ctx, task = make_task(project)
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"), project=project)
    ctx.task_svc.update(task.id, status="in_progress")
    step = ctx.task_svc.get(task.id).workflow_step
    _backdate(ctx, task.id, 600, step)

    calls: list[int] = []

    def _never_advances(prompt, **kw):
        calls.append(1)
        return _FakeResult("", exit_code=1)   # unusable -> flow_report fails

    monkeypatch.setattr(claude_cli, "invoke", _never_advances)

    # Two stalls burn the two-attempt budget.
    for _ in range(2):
        _backdate(ctx, task.id, 600, step)
        ra.sweep_once_for(project)

    assert len(calls) == 2, f"expected exactly 2 dispatch invocations, got {len(calls)}"

    # A third stall must NOT dispatch again -- budget is spent.
    _backdate(ctx, task.id, 600, step)
    ra.sweep_once_for(project)

    assert len(calls) == 2, (
        "the actuator dispatched a 3rd time past its own retry budget")

    parked = ctx.task_svc.get(task.id)
    assert parked.status == "blocked", (
        f"budget-spent task must be parked blocked, got status={parked.status!r}")
    assert "2" in (parked.blocked_reason or ""), (
        f"blocked_reason must name the retry budget/count spent; got "
        f"{parked.blocked_reason!r}")

    park_rows = [r for r in ctx.task_svc.history(task.id)
                if r.action == ra.PARKED_ACTION]
    assert len(park_rows) == 1, (
        f"expected exactly one {ra.PARKED_ACTION!r} history row; got "
        f"{[r.action for r in ctx.task_svc.history(task.id)]}")


def test_advancing_dispatch_resets_the_budget(make_task, monkeypatch):
    """A dispatch that genuinely advances the workflow_step must reset the
    attempt counter -- a task that stalls once, recovers, then stalls again
    later is a FRESH stall, not attempt N+1 of the earlier one."""
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.services import resume_actuator as ra
    from prism_service.services import resume_attempts_data as rad

    project = _project()
    ctx, task = make_task(project)
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"), project=project)
    ctx.task_svc.update(task.id, status="in_progress")
    step = ctx.task_svc.get(task.id).workflow_step
    _backdate(ctx, task.id, 600, step)

    scores_db = str(get_project_scores_db(project))
    rad.record_attempt(scores_db, task.id)
    rad.record_attempt(scores_db, task.id)
    assert rad.attempt_count(scores_db, task.id) == 2

    # This dispatch succeeds and advances the step -- known-good proof text
    # for review_previous_notes/premise_grounded (test_runner_usage_persisted.py).
    monkeypatch.setattr(
        claude_cli, "invoke",
        lambda prompt, **kw: _FakeResult("## Premises\n- ok - UNVERIFIED\n"))

    ra.dispatch_once(project, task.id)

    assert rad.attempt_count(scores_db, task.id) == 0, (
        "a dispatch that advances workflow_step must reset the retry budget")


def get_project_scores_db(project):
    from prism_service.project_context import get_project
    return get_project(project)._data_dir / "scores.db"


# ---------------------------------------------------------------------------
# AC-7 -- the seat is useless unwired. Found live auditing this task's own
# green_gate (2026-08-22): sweep_once/sweep_once_for had no caller anywhere
# outside this test file, so none of AC-1..AC-6 above ever ran against a
# real task -- same failure class as epic 0784729f ("a slice whose tests
# inject the collaborator it depends on can go green while the assembled
# product is dead"). Mirrors test_ship_worker.py's identical AC-7 trio.
# ---------------------------------------------------------------------------


def test_worker_is_off_by_default(monkeypatch):
    from prism_service.services import resume_actuator as ra

    monkeypatch.delenv("PRISM_RESUME_ACTUATOR_INTERVAL", raising=False)
    assert ra.is_enabled() is False
    assert ra.start_resume_actuator() is None, (
        "mirrors start_task_runner/start_gate_adjudicator: no opt-in, no "
        "thread, no cost")


def test_env_opts_the_environment_in(monkeypatch):
    from prism_service.services import resume_actuator as ra

    monkeypatch.setenv("PRISM_RESUME_ACTUATOR_INTERVAL", "30")
    assert ra.is_enabled() is True


def test_lifespan_wires_the_worker():
    """The seat is useless unwired -- same guard the ship_worker/task_runner
    suites keep."""
    from pathlib import Path

    import prism_service.main as m

    src = Path(m.__file__).read_text(encoding="utf-8")
    assert "start_resume_actuator" in src, (
        "start_resume_actuator() must be called from the real lifespan, or "
        "sweep_once/sweep_once_for never run against a live task")
