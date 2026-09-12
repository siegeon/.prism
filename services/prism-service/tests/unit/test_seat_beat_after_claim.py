"""A seat beats only after it holds the claim (task b490fabc, 2026-09-11).

LIVE INCIDENT: 06:14-06:37 UTC on task b490fabc. `prism-task-runner`
leased the task (30-min lease) and was killed by a daemon restart before
releasing -- `released_at` stayed NULL. `resume_actuator.dispatch_once`
used to write its DISPATCH_ACTION history row and beat `drive_heartbeat`
BEFORE calling `claim.acquire`, so every 180s sweep wrote a beat for work
that never ran, then found the claim held and deferred. Meanwhile
`task_runner` read that beat as "resume-actuator is live on it" and
skipped too. Two seats, each yielding to the other's ghost, for the whole
lease -- four `resume_actuator_dispatch` history rows and zero
`agent_runs`/`claude -p` behind them.

Fix: acquire the claim FIRST; beat and record DISPATCH_ACTION only once
it is held. A deferred attempt (claim already held elsewhere) writes
neither.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


def _project():
    return "seat-beat-" + uuid.uuid4().hex[:8]


def _backdate(ctx, task_id, seconds_ago, to):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    ctx.task_svc._db.execute(
        "INSERT INTO task_history (task_id, actor, action, details, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, "", "advance_task", f"to={to}", ts),
    )
    ctx.task_svc._db.commit()


def _make_started_task(project):
    from prism_service.api import conductor_flow as cf
    from prism_service.project_context import get_project

    ctx = get_project(project)
    task = ctx.task_svc.create(title="seat beat ordering task")
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"), project=project)
    ctx.task_svc.update(task.id, status="in_progress")
    step = ctx.task_svc.get(task.id).workflow_step
    _backdate(ctx, task.id, 600, step)
    return ctx, task


# ---------------------------------------------------------------------------
# (a) claim already held elsewhere -> deferred, NO heartbeat, NO dispatch row
# ---------------------------------------------------------------------------

def test_deferred_dispatch_writes_no_heartbeat_and_no_history_row():
    from prism_service.services import drive_heartbeat
    from prism_service.services import resume_actuator as ra
    from prism_service.services import task_runner as tr

    project = _project()
    ctx, task = _make_started_task(project)

    scores_db = ra._scores_db_for(project)
    claim = tr._claim_service(project)
    other_claim_id = claim.acquire(task.id, holder_id="prism-task-runner", ttl_s=900)
    assert other_claim_id, "setup: another seat must actually hold the lease"

    result = ra.dispatch_once(project, task.id)

    assert result.get("deferred") is True, result
    assert "held by" in (result.get("reason") or ""), result

    rows = ctx.task_svc.history(task.id)
    dispatch_rows = [r for r in rows if r.action == ra.DISPATCH_ACTION]
    assert dispatch_rows == [], (
        f"a deferred attempt must write NO {ra.DISPATCH_ACTION!r} row; "
        f"got {[r.action for r in rows]}")

    beat = drive_heartbeat.latest(scores_db, task.id)
    assert beat is None, (
        f"a deferred attempt must write NO heartbeat at all; got {beat!r}")


def test_deferred_dispatch_does_not_charge_the_total_dispatch_ceiling():
    """The 12-dispatch ceiling counts DISPATCH_ACTION rows -- a deferral
    that writes none must never move the task toward it."""
    from prism_service.services import resume_actuator as ra
    from prism_service.services import task_runner as tr

    project = _project()
    ctx, task = _make_started_task(project)

    claim = tr._claim_service(project)
    claim.acquire(task.id, holder_id="prism-task-runner", ttl_s=900)

    for _ in range(3):
        ra.dispatch_once(project, task.id)

    assert ra._total_dispatches(project, task.id) == 0


# ---------------------------------------------------------------------------
# (b) acquirable task -> beat fires AFTER acquire, not before
# ---------------------------------------------------------------------------

def test_dispatch_beats_after_acquiring_the_claim(monkeypatch):
    from prism_service.inference import claude_cli
    from prism_service.services import drive_heartbeat
    from prism_service.services import resume_actuator as ra
    from prism_service.services import task_runner as tr

    project = _project()
    ctx, task = _make_started_task(project)

    order: list[str] = []

    real_claim = tr._claim_service(project)

    class _RecordingClaim:
        def acquire(self, *a, **kw):
            order.append("acquire")
            return real_claim.acquire(*a, **kw)

        def release(self, *a, **kw):
            return real_claim.release(*a, **kw)

        def holder_of(self, *a, **kw):
            return real_claim.holder_of(*a, **kw)

    monkeypatch.setattr(tr, "_claim_service", lambda project: _RecordingClaim())

    real_beat = drive_heartbeat.record_heartbeat

    def _recording_beat(scores_db, payload):
        order.append("beat")
        return real_beat(scores_db, payload)

    monkeypatch.setattr(drive_heartbeat, "record_heartbeat", _recording_beat)
    monkeypatch.setattr(
        claude_cli, "invoke", lambda prompt, **kw: _FakeResult("", exit_code=1))

    ra.dispatch_once(project, task.id)

    assert order == ["acquire", "beat"], (
        f"the claim must be acquired BEFORE the heartbeat fires; got {order}")


class _FakeResult:
    def __init__(self, text: str, exit_code: int = 0, run_id: str = "run-seat-beat"):
        self._text = text
        self.exit_code = exit_code
        self.run_id = run_id
        self.usage = {}

    def final_text(self) -> str:
        return self._text
