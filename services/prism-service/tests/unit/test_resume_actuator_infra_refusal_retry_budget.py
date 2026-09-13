"""An infrastructure refusal never spends the resume-actuator's retry
budget, and a park it DID cause because of that bug re-arms once the
cause is gone (ops incident task a65c66e5, 2026-09-13).

Live: task a65c66e5 hit "workspace unavailable, refusing to start (fail
closed): ..." on every resume_actuator tick after its worktree was reaped
out from under it (task_reaper.sweep_worktrees) -- a pure infrastructure
refusal, not any failure of the work itself. `dispatch_once`'s `_no_advance`
charged the retry budget on EVERY refusal alike, so three infra refusals
in a row spent the whole budget and parked the task with
"resume-actuator: retry budget spent (3/3) -- parked for a human", even
though 7.13.330 already fixed the underlying workspace-recovery bug.
`_park`'s message named no cause and no remedy, and nothing in this module
could ever lift that park short of a human calling `release()` by hand --
the exact "a park 'for a human' with no human affordance is a defect"
shape.

Covers:
  AC-1 -- a dispatch refusal classified as infrastructure (workspace
          unavailable / fail closed) does NOT call `record_attempt`; the
          budget stays at 0 across N such refusals and the task is never
          parked.
  AC-2 -- a refusal from the work itself (a real dispatch that ran and
          failed) DOES spend the budget as before, and the resulting park
          reason names the refusal class, the last reason string, and the
          PRISM_VERSION at park time -- never the bare "parked for a
          human" with no diagnosable cause.
  AC-3 -- a task this seat parked for what is now a demonstrably-cleared
          infrastructure cause (the workspace resolves again) re-arms
          automatically on the next sweep: budget reset, status back to
          in_progress, and a history row saying so -- without any human
          action, mirroring the exact a65c66e5 recovery path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

SEAT = "prism-resume-actuator"


def _project():
    return "resume-infra-" + uuid.uuid4().hex[:8]


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


def _scores_db(project):
    from prism_service.project_context import get_project
    return str(get_project(project)._data_dir / "scores.db")


# ---------------------------------------------------------------------------
# AC-1 -- an infra refusal never charges the retry budget
# ---------------------------------------------------------------------------

def test_workspace_unavailable_refusal_never_spends_the_budget(make_task, monkeypatch):
    from prism_service.services import resume_actuator as ra
    from prism_service.services import resume_attempts_data as rad
    from prism_service.services import task_workspace as tw

    monkeypatch.setenv("PRISM_RESUME_ACTUATOR_MAX_RETRIES", "3")

    project = _project()
    ctx, task = make_task(project)
    ctx.task_svc.update(task.id, status="in_progress",
                        workflow_step="write_failing_tests")
    step = ctx.task_svc.get(task.id).workflow_step
    _backdate(ctx, task.id, 600, step)

    def _fail_closed(*a, **kw):
        raise RuntimeError("fatal: unable to read tree (some git error)")

    monkeypatch.setattr(tw, "ensure_workspace", _fail_closed)

    scores_db = _scores_db(project)
    for _ in range(5):
        _backdate(ctx, task.id, 600, step)
        res = ra.sweep_once_for(project)
        assert res is not None and res.get("ok") is False, res
        assert "workspace unavailable" in (res.get("reason") or ""), res

    assert rad.attempt_count(scores_db, task.id) == 0, (
        "an infrastructure refusal (workspace unavailable, fail closed) "
        "must never spend the retry budget -- got "
        f"{rad.attempt_count(scores_db, task.id)}")

    refreshed = ctx.task_svc.get(task.id)
    assert refreshed.status == "in_progress", (
        f"a task that only ever hit infra refusals must never be parked; "
        f"got status={refreshed.status!r}, blocked_reason="
        f"{refreshed.blocked_reason!r}")


# ---------------------------------------------------------------------------
# AC-2 -- a real step failure still spends the budget and parks with a
# diagnosable reason
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, text: str, exit_code: int = 0, run_id: str = "run-resume"):
        self._text = text
        self.exit_code = exit_code
        self.run_id = run_id
        self.usage = {}

    def final_text(self) -> str:
        return self._text

    def graceful_budget_stop(self) -> bool:
        return False


def test_real_step_failure_spends_budget_and_parks_with_named_cause(make_task, monkeypatch):
    from prism_service.__version__ import PRISM_VERSION
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

    monkeypatch.setattr(claude_cli, "invoke",
                        lambda prompt, **kw: _FakeResult("", exit_code=1))

    for _ in range(2):
        _backdate(ctx, task.id, 600, step)
        ra.sweep_once_for(project)

    _backdate(ctx, task.id, 600, step)
    ra.sweep_once_for(project)

    parked = ctx.task_svc.get(task.id)
    assert parked.status == "blocked", parked
    reason = parked.blocked_reason or ""
    assert "class=work" in reason, (
        f"park reason must name the refusal class so a re-arm sweep can "
        f"tell a genuine work failure from an infra one; got {reason!r}")
    assert "no usable output" in reason or "exit=" in reason, (
        f"park reason must carry the exact last-attempt reason string, "
        f"never a bare 'parked for a human'; got {reason!r}")
    assert f"parked_version={PRISM_VERSION}" in reason, (
        f"park reason must record the PRISM_VERSION at park time so a "
        f"later sweep can tell whether the daemon has since changed; "
        f"got {reason!r}")


# ---------------------------------------------------------------------------
# AC-3 -- a park caused by (legacy, unclassified) infra refusals re-arms
# once the workspace resolves again -- the exact a65c66e5 shape.
# ---------------------------------------------------------------------------

def test_legacy_infra_park_rearms_once_workspace_resolves(make_task):
    from prism_service.services import resume_actuator as ra
    from prism_service.services import resume_attempts_data as rad

    project = _project()
    ctx, task = make_task(project)
    ctx.task_svc.update(task.id, status="in_progress",
                        workflow_step="write_failing_tests")
    step = ctx.task_svc.get(task.id).workflow_step
    _backdate(ctx, task.id, 600, step)

    # Simulate the exact pre-fix state: a park with the OLD, unclassified
    # message (no class=/parked_version= tags at all) -- precisely what
    # task a65c66e5's real blocked_reason looks like today.
    legacy_reason = "resume-actuator: retry budget spent (3/3) — parked for a human"
    ctx.task_svc.update(task.id, status="blocked", blocked_reason=legacy_reason)
    scores_db = _scores_db(project)
    rad.record_attempt(scores_db, task.id)
    rad.record_attempt(scores_db, task.id)
    rad.record_attempt(scores_db, task.id)
    assert rad.attempt_count(scores_db, task.id) == 3

    # The underlying cause is gone now (ensure_workspace succeeds again,
    # e.g. after 7.13.330's recovery fix) -- untouched real implementation,
    # not mocked, since a real task workspace is created by the make_task
    # fixture's own project context.
    res = ra.sweep_once_for(project)
    assert res is not None and res.get("ok") is True, res

    rearmed = ctx.task_svc.get(task.id)
    assert rearmed.status == "in_progress", (
        f"a legacy infra-caused park must self-heal once its cause "
        f"clears, without any human action; got status={rearmed.status!r}, "
        f"blocked_reason={rearmed.blocked_reason!r}")
    assert rad.attempt_count(scores_db, task.id) == 0, (
        "re-arming must reset the spent budget")

    rows = ctx.task_svc.history(task.id)
    rearm_rows = [r for r in rows if r.action == ra.RELEASED_ACTION
                 and "re-armed" in (r.details or "")]
    assert len(rearm_rows) == 1, (
        f"expected exactly one RELEASED_ACTION row naming the re-arm; got "
        f"{[(r.action, r.details) for r in rows]}")
