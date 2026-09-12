"""The handoff must stand down for a live SESSION driver (task ad38e421).

Found 2026-09-11 (memory project-daemon-takes-session-drives): when a
session's `conductor_work` report advances a task onto an AGENT step while
the task is `in_progress`, `dispatch.after_step` calls `_drive_now`, which
calls `task_runner._run_one_step` in a thread in the SAME SECOND. That path
never asked whether anybody else was already on the task -- the only
existing check, `task_runner._foreign_driver_on`, was wired into
`eligible_tasks` (the periodic sweep) and nowhere else. A session driving a
task over the REST flow posts drive heartbeats exactly like task_runner
does (POST /api/drive-heartbeat/beat), under its OWN driver name -- so a
live one is exactly as much "somebody is on this" evidence for the instant
handoff as it already is for the sweep.

Seen live: on 4d86db87 the local model overwrote a session's 8-AC story
within 2s. On 87cb620e the daemon ran draft_story mid-drive and wrote a
`<think>` preamble into plan_doc.

The likely misfire this pins against: only `_drive_now` gets guarded and
`on_started` (or a sweep) still takes the step; or the runner reads its own
stale beat as foreign and never drives again; or the check reads a
DIFFERENT task's beat.
"""

from __future__ import annotations

import uuid

import pytest


def _project() -> str:
    return "handoff-live-driver-" + uuid.uuid4().hex[:8]


class _Task:
    def __init__(self, tid, status="pending", deps=None, step="", parent=""):
        self.id = tid
        self.status = status
        self.dependencies = deps or []
        self.workflow_step = step
        self.parent_id = parent


class _Svc:
    def __init__(self, rows):
        self._rows = {t.id: t for t in rows}

    def get(self, tid):
        return self._rows.get(tid)

    def list(self, status="", parent_id=None, **_):
        out = list(self._rows.values())
        if status:
            out = [t for t in out if t.status == status]
        if parent_id is not None:
            out = [t for t in out if t.parent_id == parent_id]
        return out


# ---------------------------------------------------------------------------
# (1) A live FOREIGN beat: after_step must not call claude_cli.invoke, and
#     must return a reason naming the driver.
# ---------------------------------------------------------------------------

def test_after_step_stands_down_for_a_live_foreign_driver(monkeypatch):
    from prism_service.services import dispatch

    driven = []
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda tid, project: driven.append(tid))
    monkeypatch.setattr(dispatch, "_foreign_driver_on",
                        lambda tid, project: "session-abc123")

    svc = _Svc([_Task("t", status="in_progress", step="implement_tasks")])
    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: svc)

    result = dispatch.after_step("t", "proj")

    assert driven == [], (
        "a task with a live foreign driver must never be handed to "
        "_drive_now (and so never reach claude_cli.invoke)")
    assert result.get("drove") is False
    assert "session-abc123" in str(result.get("reason") or ""), (
        f"the refusal must name the live driver; got {result!r}")


def test_on_started_also_stands_down_for_a_live_foreign_driver(monkeypatch):
    """likely_misfire: 'only _drive_now is guarded, and on_started or a
    sweep still takes the step' -- on_started is the other caller of
    _drive_now and must be guarded identically."""
    from prism_service.services import dispatch

    driven = []
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda tid, project: driven.append(tid))
    monkeypatch.setattr(dispatch, "_foreign_driver_on",
                        lambda tid, project: "session-xyz")

    svc = _Svc([_Task("t", status="in_progress", step="")])
    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: svc)

    result = dispatch.on_started("t", "proj")

    assert driven == []
    assert result.get("drove") is False
    assert "session-xyz" in str(result.get("reason") or "")


# ---------------------------------------------------------------------------
# (2) No beat, or a stale one: drives the step exactly as before.
# ---------------------------------------------------------------------------

def test_after_step_still_drives_with_no_foreign_driver(monkeypatch):
    from prism_service.services import dispatch

    driven = []
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda tid, project: driven.append(tid))
    monkeypatch.setattr(dispatch, "_foreign_driver_on",
                        lambda tid, project: "")

    svc = _Svc([_Task("t", status="in_progress", step="implement_tasks")])
    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: svc)

    result = dispatch.after_step("t", "proj")

    assert driven == ["t"], (
        "with no live foreign driver the step must still be driven "
        "immediately, exactly as before this guard existed")
    assert result.get("drove") is True


def test_foreign_driver_on_delegates_to_task_runners_own_check(monkeypatch):
    """There must be exactly ONE definition of 'somebody else is driving
    this' -- dispatch.py must not grow a second, divergent copy of the
    beat/age/driver-name logic that eligible_tasks already uses."""
    from prism_service.services import dispatch, task_runner

    calls = []

    def _fake(project, task_id):
        calls.append((project, task_id))
        return "some-driver"

    monkeypatch.setattr(task_runner, "_foreign_driver_on", _fake)

    result = dispatch._foreign_driver_on("t", "proj")

    assert result == "some-driver"
    assert calls == [("proj", "t")], (
        "dispatch._foreign_driver_on must delegate to "
        "task_runner._foreign_driver_on with (project, task_id), not "
        "reimplement the beat/age check itself")


# ---------------------------------------------------------------------------
# (3) task_runner._run_one_step called directly with a live foreign beat
#     returns without ever reaching dispatch_guard's chokepoint -- no
#     dispatch_guard_dispatch history row, and no claude_cli.invoke.
# ---------------------------------------------------------------------------

@pytest.fixture()
def make_task():
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(project, **kwargs):
        ctx = get_project(project)
        task = ctx.task_svc.create(
            title=kwargs.pop("title", "handoff-guard task"), **kwargs)
        created.append(task.id)
        return ctx, task

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


def test_run_one_step_refuses_a_live_foreign_beat_with_no_dispatch_row(
        make_task, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.services import dispatch_guard, drive_heartbeat
    from prism_service.services import task_runner as tr

    project = _project()
    ctx, task = make_task(project)

    # Enter the flow once as a real driver would, to get a real workflow_step
    # and workspace -- exactly the shape task_runner would see live.
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"), project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    # A SESSION is live on this task: a fresh heartbeat under a driver name
    # that is not the runner's own.
    scores_db = tr._scores_db_for(project)
    drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task.id, "step": ctx.task_svc.get(task.id).workflow_step,
        "elapsed_s": 5, "last_tool": "conductor_work", "work_units": 1,
        "driver": "session-live-driver",
    })

    invoked = []

    def _tripwire(prompt, **kw):
        invoked.append(kw)
        raise AssertionError(
            "claude_cli.invoke must never be called while a session is "
            "live on this task")

    monkeypatch.setattr(claude_cli, "invoke", _tripwire)

    before_history = len(ctx.task_svc.history(task.id) or [])

    result = tr._run_one_step(project, task.id)

    assert not invoked, f"invoke must not be called; got {invoked}"
    assert result.get("ok") is False
    assert "session-live-driver" in str(result.get("reason") or ""), result

    after_history = ctx.task_svc.history(task.id) or []
    new_actions = [str(getattr(r, "action", "")) for r in after_history[before_history:]]
    assert dispatch_guard.DISPATCH_ACTION not in new_actions, (
        f"a refused-before-the-chokepoint drive must never write a "
        f"{dispatch_guard.DISPATCH_ACTION!r} row; got {new_actions!r}")
