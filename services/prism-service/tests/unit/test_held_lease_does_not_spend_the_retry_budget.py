"""A held drive lease must not spend the resume actuator's retry budget
(task ce471e06, 2026-09-04).

`dispatch_once` takes the same lease every driver takes. When another
driver already holds it, the seat returned through `_no_advance`, which
RECORDS A RETRY ATTEMPT — so bouncing off a healthy driver counted
exactly like a failed drive.

LIVE REGRESSION: `verify_green_state` runs ~25 minutes and holds a
45-minute lease, while this seat sweeps every 180 s. Three sweeps landed
inside one legitimately-running step, each bounced off the lease, each
charged an attempt, and the third PARKED the task with "retry budget
spent (3/3) — parked for a human" — killing a step that was working
normally. Timestamps on ce471e06: dispatches at 20:01:36, 20:04:36 and
20:07:36, parked at 20:10:36.

A held lease is evidence that work IS happening — the opposite of the
stall this seat exists to rescue. It now defers without charging an
attempt. Every OTHER non-advance (no workspace, refused flow_start, a
gate/terminal job) still charges one, so this narrows the rule rather
than disabling the budget.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture()
def make_task():
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make():
        project = "lease-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        task = ctx.task_svc.create(title="leased task")
        created.append(task.id)
        return ctx, task, project

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


class _HeldClaim:
    """A claim service whose lease is already held by another driver."""

    def acquire(self, task_id, holder_id=None, ttl_s=None):
        return None

    def holder_of(self, task_id):
        return "prism-task-runner"

    def release(self, claim_id):  # pragma: no cover - never reached
        raise AssertionError("nothing to release when acquire failed")


def _start(ctx, task, project):
    from prism_service.api import conductor_flow as cf

    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                  project=project)
    ctx.task_svc.update(task.id, status="in_progress")


def test_bouncing_off_a_held_lease_charges_no_attempt(make_task, monkeypatch):
    """The live failure: three sweeps inside one long step must leave the
    retry budget untouched, so the task is never parked for working."""
    from prism_service.services import resume_actuator as ra
    from prism_service.services import resume_attempts_data as rad
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    _start(ctx, task, project)
    scores_db = ra._scores_db_for(project)
    monkeypatch.setattr(tr, "_claim_service", lambda p: _HeldClaim())

    for _ in range(3):
        res = ra.dispatch_once(project, task.id)
        assert res.get("ok") is not True
        assert "already driving" in (res.get("reason") or "")

    assert rad.attempt_count(scores_db, task.id) == 0, (
        "a held lease is evidence a driver is working — it must not spend "
        "the retry budget that parks the task")
    assert ctx.task_svc.get(task.id).status == "in_progress", (
        "the task must not be parked while another driver holds its lease")


def test_a_held_lease_reports_itself_as_deferred(make_task, monkeypatch):
    """The caller can tell 'deferred, someone else is on it' apart from a
    real failed drive — the sweep and any reader need that distinction."""
    from prism_service.services import resume_actuator as ra
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    _start(ctx, task, project)
    monkeypatch.setattr(tr, "_claim_service", lambda p: _HeldClaim())

    res = ra.dispatch_once(project, task.id)

    assert res.get("deferred") is True
    assert res.get("step")


def test_a_real_non_advance_still_charges_an_attempt(make_task, monkeypatch):
    """The budget still exists: a non-advance that is NOT a held lease
    (here: no workspace on file) charges an attempt exactly as before."""
    from prism_service.services import resume_actuator as ra
    from prism_service.services import resume_attempts_data as rad
    from prism_service.services import task_workspace

    ctx, task, project = make_task()
    _start(ctx, task, project)
    scores_db = ra._scores_db_for(project)
    monkeypatch.setattr(task_workspace, "workspace_for", lambda tid: {})

    res = ra.dispatch_once(project, task.id)

    assert res.get("ok") is not True
    assert res.get("deferred") is not True
    assert rad.attempt_count(scores_db, task.id) == 1, (
        "only a HELD LEASE is exempt; every other non-advance still spends "
        "the budget")
