"""A parallel runner sweep must not clobber a live session's plan_doc
(task 7180de77).

Companion defect to [[project-daemon-takes-session-drives]] / task ad38e421:
even where the INSTANT handoff (dispatch.after_step/_drive_now) is guarded,
task_runner's own periodic sweep drives `_run_one_step` on any task it
still considers eligible. Without a guard AT the step-execution chokepoint
itself, a session mid-drive (writing plan_doc via its own conductor_work
report) can race a runner sweep that reports the SAME step with older or
empty material and overwrites it -- measured live on 4d86db87 (an 8-AC
story overwritten within 2s) and 87cb620e (a `<think>` preamble written
into plan_doc mid-drive).

The guard is task_runner._foreign_driver_on, now checked at the very top
of _run_one_step -- before flow_start, before the runner's own heartbeat,
before anything that could report and touch plan_doc.
"""

from __future__ import annotations

import uuid

import pytest


def _project() -> str:
    return "runner-live-drive-" + uuid.uuid4().hex[:8]


@pytest.fixture()
def make_task():
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(project, **kwargs):
        ctx = get_project(project)
        task = ctx.task_svc.create(
            title=kwargs.pop("title", "live-drive task"), **kwargs)
        created.append(task.id)
        return ctx, task

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


def test_runner_skips_heartbeating_tasks(make_task, monkeypatch):
    """With the runner enabled and a linked session actively heartbeating a
    task: the runner does not report steps on that task, and a drive's
    plan_doc survives a parallel runner sweep verbatim."""
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.services import drive_heartbeat
    from prism_service.services import task_runner as tr

    project = _project()
    ctx, task = make_task(project)

    # A session drives this task into the flow and writes a real plan_doc,
    # exactly as conductor_work would on a live report.
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"), project=project)
    ctx.task_svc.update(task.id, status="in_progress")
    step = ctx.task_svc.get(task.id).workflow_step
    assert step, "the task must have entered the flow to have a real step"

    sentinel_plan = "# The session's own plan\n\nThis must survive verbatim."
    ctx.task_svc.update(task.id, plan_doc=sentinel_plan)
    # Capture what the store actually persisted (STE alignment may reflow
    # ordinary prose) rather than assuming the literal input survives --
    # the guard under test is "unchanged by a parallel sweep", not "the
    # store never touches plan_doc at write time".
    stored_plan = ctx.task_svc.get(task.id).plan_doc
    assert stored_plan

    # The session is ACTIVELY heartbeating -- a fresh beat under its own
    # driver name, exactly what a live conductor_work drive posts.
    scores_db = tr._scores_db_for(project)
    drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task.id, "step": step, "elapsed_s": 3,
        "last_tool": "conductor_work", "work_units": 1,
        "driver": "session-parallel-drive",
    })

    def _tripwire(prompt, **kw):
        raise AssertionError(
            "a parallel runner sweep must never invoke the model while a "
            "session is actively heartbeating the same task")

    monkeypatch.setattr(claude_cli, "invoke", _tripwire)

    result = tr._run_one_step(project, task.id)

    assert result.get("ok") is False, (
        "the runner's report on this task must be refused, not silently "
        f"no-opped; got {result!r}")
    assert "session-parallel-drive" in str(result.get("reason") or ""), (
        f"the refusal must name the live driver; got {result!r}")

    refreshed = ctx.task_svc.get(task.id)
    assert refreshed.plan_doc == stored_plan, (
        "the session's plan_doc must survive a parallel runner sweep "
        f"verbatim; got {refreshed.plan_doc!r}")
