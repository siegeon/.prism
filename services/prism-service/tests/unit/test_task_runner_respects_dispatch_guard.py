"""End-to-end pin: task_runner's real dispatch path honors the shared
dispatch guard (task ab9166d5 incident, 2026-09-10).

Live measurement: task ab9166d5 was parked `blocked` by a governance seat's
ceiling, and a fresh `claude -p` child kept being spawned for it anyway,
minutes apart, for 6+ hours. This is the compounding shape of the bug:

  1. `task_runner._run_one_step` called `flow.flow_start(...)` first, whose
     own `_mark_in_progress` used to flip ANY pending/blocked task straight
     back to in_progress -- unconditionally, however it got parked.
  2. Nothing downstream re-checked task status before the real
     `claude_cli.invoke()` call, so a task a governance seat had just
     stopped was invoked again anyway.

Both halves are required for the fix to hold: `_mark_in_progress` must
stop silently erasing a governance park (conductor_flow.py), AND
`_run_one_step` must re-check status FRESH at the true chokepoint, right
before the GPU spends anything (dispatch_guard.try_begin). Either one
alone is not enough -- see the docstrings on both.
"""

from __future__ import annotations

import uuid

import pytest


def _project() -> str:
    return "task-runner-guard-" + uuid.uuid4().hex[:8]


@pytest.fixture()
def make_task():
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(project, **kwargs):
        ctx = get_project(project)
        task = ctx.task_svc.create(
            title=kwargs.pop("title", "guarded task"), **kwargs)
        created.append(task.id)
        return ctx, task

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


def test_run_one_step_never_invokes_a_governance_parked_task(make_task, monkeypatch):
    """The exact incident, reproduced: a task already at implement_tasks,
    parked blocked by dispatch_guard's own ceiling, called directly the
    way task_runner's sweep would -- must never reach claude_cli.invoke,
    and must not be silently resurrected to in_progress along the way."""
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.services import task_runner as tr

    project = _project()
    ctx, task = make_task(project)

    # Enter the flow once, as a real driver would, to get a real
    # workflow_step and workspace -- then park it exactly as
    # dispatch_guard.try_begin's own ceiling refusal would.
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"), project=project)
    ctx.task_svc.update(task.id, status="in_progress")
    step = ctx.task_svc.get(task.id).workflow_step
    assert step, "the task must have entered the flow before it can be parked mid-step"

    ctx.task_svc.update(
        task.id, status="blocked",
        blocked_reason=f"dispatch-guard: 12 dispatches for this task, at "
                       "the ceiling of 12. Refusing to dispatch again -- "
                       "parked for a person.")

    invoked = []

    def _tripwire(prompt, **kw):
        invoked.append(kw)
        raise AssertionError(
            "claude_cli.invoke must never be called for a task that is "
            "not in_progress -- however it got selected for this call")

    monkeypatch.setattr(claude_cli, "invoke", _tripwire)

    result = tr._run_one_step(project, task.id)

    assert not invoked, (
        f"the GPU must never be spent on a governance-parked task; "
        f"invoke was called with {invoked}")
    assert result.get("ok") is False, result

    refreshed = ctx.task_svc.get(task.id)
    assert refreshed.status == "blocked", (
        "the task must still read blocked afterward -- not silently "
        f"resurrected along the way; got status={refreshed.status!r}")
