"""A REST-driven task must SHOW as working, and must not attract a second
driver (task e4c631d7).

Owner 2026-08-30: "i dont see you or any sub agent playing the task in the
app?" -- while task 54585a5f had already cleared story_gate, plan_gate and
red_gate and carried two commits on its own branch.

CONFIRMED CAUSE. Two drive paths exist and only ONE tells the board that
work started. `mcp/tools.py:_mark_in_progress` flips a pending task to
in_progress on every conductor_work call. The REST flow used by the
implement workflow and by every HTTP-driving subagent
(`api/conductor_flow.py` flow_start / flow_report) never did. So a
REST-driven task advanced through four steps with status still `pending`,
and `conductor_service.activity_for` short-circuits to state "pending" on
that status BEFORE it reads motion, heartbeat or session quiet -- the tile
rendered idle backlog while the conductor was moving the task.

SECOND HALF, which must ship in the same change. `task_runner.eligible_task`
selects purely on status=in_progress + an agent step, and knows nothing
about an external live driver. Marking REST drives in_progress therefore
lets the daemon start a SECOND driver on a task a session is already
driving (the d9f082fe "two queues of control" failure). The proof of a live
driver already exists -- drive_heartbeat -- so the runner reads it, and
skips a task whose fresh beat came from somebody else.

Pinned here:
  AC-1 -- flow_report on a PENDING task at an agent step marks it in_progress.
  AC-2 -- flow_start marks a pending task in_progress.
  AC-3 -- a terminal (done) task is NEVER resurrected by a report.
  AC-4 -- eligible_task skips a task carrying a FRESH foreign drive heartbeat.
  AC-5 -- eligible_task still returns a task whose beat is the RUNNER'S OWN,
          so the daemon's autonomous loop keeps driving its own work.
  AC-6 -- eligible_task still returns a task with no heartbeat at all.
"""

from __future__ import annotations

import uuid

_SEAT = "rest-drive-visibility-session"


def _project() -> str:
    return "restvis-" + uuid.uuid4().hex[:8]


def test_a_reported_step_marks_a_pending_task_in_progress():
    """AC-1: the owner must SEE the drive. A pass report on a pending task
    at an agent step flips it to in_progress, so activity_for stops
    short-circuiting to state "pending"."""
    from prism_service.api import conductor_flow as flow
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    project = _project()
    ctx = get_project(project)
    _ = ctx.conductor_svc

    task = ctx.task_svc.create(title="a task driven over REST")
    # EXACTLY the shape observed live on 54585a5f: the conductor had
    # advanced it four steps and the status was still pending.
    ctx.task_svc.update(task.id, status="pending",
                        workflow_step="implement_tasks", gate_state="none")
    try:
        flow.flow_report(
            flow.Ident(task_id=task.id, session_id=_SEAT, outcome="pass",
                       proof="did the thing", expected_step="implement_tasks"),
            project=project,
        )
        after = ctx.task_svc.get(task.id)
        assert after.status == "in_progress", (
            "a REST-driven task must read as WORKING on the board; the MCP "
            "path already does this via _mark_in_progress, the REST path "
            f"left it pending -- got status={after.status!r}")
    finally:
        tw.remove_workspace(task.id)


def test_flow_start_marks_a_pending_task_in_progress():
    """AC-2: visibility must begin at the FIRST call, not at the first
    successful report -- an intake window that reads pending is the same
    blind spot one step earlier."""
    from prism_service.api import conductor_flow as flow
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    project = _project()
    ctx = get_project(project)
    _ = ctx.conductor_svc

    task = ctx.task_svc.create(title="a task about to start over REST")
    try:
        res = flow.flow_start(
            flow.Ident(task_id=task.id, session_id=_SEAT), project=project)
        if not res.get("ok"):
            import pytest
            pytest.skip(f"workspace unavailable in this env: {res!r}")
        after = ctx.task_svc.get(task.id)
        assert after.status == "in_progress", (
            "flow_start claims the task for a driver; the board must say so "
            f"-- got status={after.status!r}")
    finally:
        tw.remove_workspace(task.id)


def test_a_done_task_is_never_resurrected_by_a_report():
    """AC-3 (the likely_misfire): marking work visible must never drag a
    terminal task back onto the board as live work."""
    from prism_service.api import conductor_flow as flow
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    project = _project()
    ctx = get_project(project)
    _ = ctx.conductor_svc

    task = ctx.task_svc.create(title="a task that already shipped")
    ctx.task_svc.update(task.id, status="done",
                        workflow_step="implement_tasks", gate_state="none")
    try:
        flow.flow_report(
            flow.Ident(task_id=task.id, session_id=_SEAT, outcome="pass",
                       proof="late report", expected_step="implement_tasks"),
            project=project,
        )
        after = ctx.task_svc.get(task.id)
        assert after.status == "done", (
            "a done task must stay done; a visibility fix that resurrects "
            f"terminal work is worse than the blind spot -- got {after.status!r}")
    finally:
        tw.remove_workspace(task.id)


def _agent_step_task(ctx, title: str):
    """An in_progress task parked on an AGENT step -- the exact shape
    eligible_task is willing to drive."""
    t = ctx.task_svc.create(title=title)
    ctx.task_svc.update(t.id, status="in_progress",
                        workflow_step="implement_tasks", gate_state="none")
    return t


def test_a_task_with_a_live_foreign_driver_is_not_eligible():
    """AC-4: the daemon must not open a second queue of control on a task a
    session is already driving (the d9f082fe failure). A fresh beat from a
    driver that is not this runner is proof somebody is on it."""
    from prism_service.project_context import get_project
    from prism_service.services import drive_heartbeat, task_runner

    project = _project()
    ctx = get_project(project)
    task = _agent_step_task(ctx, "a task a session is already driving")

    drive_heartbeat.record_heartbeat(task_runner._scores_db_for(project), {
        "task_id": task.id, "step": "implement_tasks", "elapsed_s": 12,
        "last_tool": "Edit", "work_units": 3,
        "driver": "drive-54585a5f",
    })

    assert task_runner.eligible_task(project) != task.id, (
        "a task carrying a FRESH heartbeat from another driver must not be "
        "claimed by the daemon -- two drivers on one task is the owner's "
        "'two queues of control' failure")


def test_the_runner_still_drives_a_task_it_beat_for_itself():
    """AC-5 (the likely_misfire): the guard must read the DRIVER, not the
    mere presence of a beat. task_runner records its own heartbeat at the
    start of every step, and a transition wakes the next tick well inside
    HEARTBEAT_WINDOW_S -- a naive guard would make the daemon skip its own
    work forever."""
    from prism_service.project_context import get_project
    from prism_service.services import drive_heartbeat, task_runner

    project = _project()
    ctx = get_project(project)
    task = _agent_step_task(ctx, "a task the daemon itself is driving")

    drive_heartbeat.record_heartbeat(task_runner._scores_db_for(project), {
        "task_id": task.id, "step": "implement_tasks", "elapsed_s": 0,
        "last_tool": "claude_cli.invoke", "work_units": 1,
        "driver": task_runner.RUNNER_DRIVER,
    })

    assert task_runner.eligible_task(project) == task.id, (
        "the runner's OWN beat must never disqualify its own task, or the "
        "autonomous drive loop stops after its first step")


def test_a_task_with_no_heartbeat_at_all_stays_eligible():
    """AC-6: the guard is additive. Absent evidence of a live driver, the
    daemon behaves exactly as before."""
    from prism_service.project_context import get_project
    from prism_service.services import task_runner

    project = _project()
    ctx = get_project(project)
    task = _agent_step_task(ctx, "a task nobody is driving")

    assert task_runner.eligible_task(project) == task.id, (
        "no heartbeat means no evidence of a driver; the runner must still "
        "pick the task up")
