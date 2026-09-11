"""A session drive keeps its step against the daemon (task ad38e421).

LIVE, 2026-09-11. A session drove task 4d86db87 through conductor_work and
posted a fresh drive heartbeat as claude-session-3e44ae1d. Its report moved
the task onto draft_story, and in the SAME SECOND the daemon dispatched that
step to the local model ("dispatch_guard_dispatch seat=prism-task-runner
step=draft_story 1/12"). The model's story replaced the session's 8-AC story
in plan_doc within 2 s, and story_gate passed the model's story.

Cause: dispatch.after_step hands every advancing report on an agent step to
_drive_now, which calls task_runner._run_one_step at once. That path never
asks _foreign_driver_on (only eligible_tasks does), and a session that
drives through conductor_work holds no ClaimService lease.

These tests run the REAL handoff and the REAL runner against a real project,
with only the model call replaced by a recorder.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

SESSION = "claude-session-abc12345"


@pytest.fixture()
def driven(monkeypatch):
    """A factory for a real task on a real AGENT step with a workspace, plus
    the list of model calls. The node-plan seams are pinned to the plain
    invoke path, so every drive that is let through reaches the model."""
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.project_context import get_project
    from prism_service.services import dispatch
    from prism_service.services import task_runner as tr
    from prism_service.services import task_workspace as tw

    invoked: list[str] = []

    def _record(prompt, **kw):
        invoked.append(kw.get("purpose", ""))
        raise RuntimeError("model call recorded, not made")

    monkeypatch.setattr(claude_cli, "invoke", _record)
    monkeypatch.setattr(tr, "_node_plan", lambda project, step: None)
    monkeypatch.setattr(tr, "_codified_step_proof", lambda s, t, f: "")
    monkeypatch.setattr(tr, "_declared_agentic_prompt",
                        lambda s, t, f, plan=None: "")
    created: list[str] = []

    def _make():
        project = "handoff-driver-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        task = ctx.task_svc.create(title="handoff under a live driver")
        created.append(task.id)
        cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                      project=project)
        ctx.task_svc.update(task.id, status="in_progress")
        step = ctx.task_svc.get(task.id).workflow_step
        assert dispatch._is_agent_step(step), step
        return project, ctx, task.id

    yield _make, invoked

    for task_id in created:
        _settle(task_id)
        tw.remove_workspace(task_id)


def _beat(project, task_id, driver, age_s=0.0):
    """One drive heartbeat. age_s backdates its progress clock, which is
    what makes a beat stale."""
    from prism_service.services import drive_heartbeat
    from prism_service.services import task_runner as tr

    db = tr._scores_db_for(project)
    drive_heartbeat.record_heartbeat(db, {
        "task_id": task_id, "step": "draft_story", "elapsed_s": 5,
        "last_tool": "conductor_work", "work_units": 1, "driver": driver})
    if age_s:
        then = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE drive_heartbeats SET last_progress_at = ? "
                         "WHERE task_id = ?", (then.isoformat(), task_id))


def _settle(task_id):
    """Wait for the handoff thread, so a drive it started is counted."""
    for t in threading.enumerate():
        if t.name == f"handoff-{task_id[:8]}":
            t.join(timeout=60)


def _dispatches(ctx, task_id):
    from prism_service.services import dispatch_guard

    return dispatch_guard._total_dispatches(ctx.task_svc, task_id)


def test_a_live_session_beat_keeps_the_step_from_the_handoff(driven):
    """Oracle (1), the 4d86db87 incident: an advancing report lands on an
    agent step while another driver's beat is live."""
    from prism_service.services import dispatch

    make, invoked = driven
    project, ctx, tid = make()
    _beat(project, tid, SESSION)

    out = dispatch.after_step(tid, project, advanced=True)
    _settle(tid)

    assert not invoked, f"the handoff reached the model: {invoked}"
    assert out.get("drove") is False, out
    assert SESSION in str(out.get("reason") or ""), (
        "the refusal must name the driver that holds the step: %r" % out)
    assert _dispatches(ctx, tid) == 0


def test_the_runner_refuses_under_a_live_foreign_beat(driven):
    """Oracle (3). One chokepoint covers _drive_now, on_started and every
    sweep: _run_one_step itself returns before it counts a dispatch."""
    from prism_service.services import task_runner as tr

    make, invoked = driven
    project, ctx, tid = make()
    _beat(project, tid, SESSION)

    out = tr._run_one_step(project, tid)

    assert out.get("ok") is False, out
    assert SESSION in str(out.get("reason") or ""), out
    assert _dispatches(ctx, tid) == 0, "a dispatch_guard_dispatch row was written"
    assert not invoked


def test_a_start_under_a_live_driver_does_not_reach_the_model(driven):
    """likely_misfire: only _drive_now is guarded, and on_started still
    takes the step."""
    from prism_service.services import dispatch

    make, invoked = driven
    project, ctx, tid = make()
    _beat(project, tid, SESSION)

    out = dispatch.on_started(tid, project)
    _settle(tid)

    assert not invoked, f"on_started reached the model: {invoked}"
    assert out.get("drove") is False, out
    assert _dispatches(ctx, tid) == 0


@pytest.mark.parametrize(
    "case", ["no_beat", "stale_beat", "own_beat", "other_task_beat"])
def test_without_a_live_foreign_driver_the_step_is_driven(driven, case):
    """Oracle (2), plus likely_misfire: the runner's OWN beat never locks it
    out, and a beat on a different task never blocks this one."""
    from prism_service.services import dispatch, drive_heartbeat
    from prism_service.services import task_runner as tr

    make, invoked = driven
    project, ctx, tid = make()
    if case == "stale_beat":
        _beat(project, tid, SESSION,
              age_s=drive_heartbeat.HEARTBEAT_WINDOW_S + 60)
    elif case == "own_beat":
        _beat(project, tid, tr.RUNNER_DRIVER)
    elif case == "other_task_beat":
        _beat(project, str(uuid.uuid4()), SESSION)

    out = dispatch.after_step(tid, project, advanced=True)
    _settle(tid)

    assert out.get("drove") is True, out
    assert len(invoked) == 1, f"the step was not driven: {invoked}"
    assert _dispatches(ctx, tid) == 1
