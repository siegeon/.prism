"""The retry seat must record WHAT ITS REPORT DID, not just that it fired.

`task_runner._run_one_step` has always written a
"step=<id>; advanced=false" history row when a report failed to move the
step. `resume_actuator.dispatch_once` never did: it kept the refusal in
its own return value and wrote only the pre-work
"seat=...; step=..." dispatch row. So a refused report left NOTHING
durable saying why.

LIVE REGRESSION this pins (2026-09-05, project prism): nine tasks sat
blocked with "resume-actuator: retry budget spent (3/3) - parked for a
human" at `review_previous_notes`. Each carried three identical dispatch
rows and no reason anywhere -- the inference had really run (6k-18k
tokens, ok=True in agent_runs), so the step was refused at report time
and neither a person nor a machine seat could see why. One of them,
1bcb2b24, was released by hand and re-parked nine minutes later.

A park that names no cause cannot be codified away, which is the whole
point of the seat.
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
def task_ctx():
    """A real project + task, so history round-trips through TaskService."""
    from prism_service.project_context import get_project

    project = "ra-report-" + uuid.uuid4().hex[:8]
    ctx = get_project(project)
    task = ctx.task_svc.create(title="retry seat report row")
    return ctx, task, project


def _rows(ctx, task_id, action):
    return [r for r in (ctx.task_svc.history(task_id) or [])
            if str(getattr(r, "action", "") or "") == action]


def test_a_refused_report_records_the_reason(task_ctx):
    from prism_service.services import resume_actuator as ra

    ctx, task, _ = task_ctx
    ra._record_report(ctx.task_svc, task.id, "review_previous_notes",
                      {"ok": False, "error": "premise_grounded: no premises"},
                      {"ok": False, "reason": "exit=0"})

    rows = _rows(ctx, task.id, ra.REPORT_ACTION)
    assert len(rows) == 1, "the seat must record what its report did"
    details = str(rows[0].details)
    assert "advanced=false" in details
    assert "premise_grounded: no premises" in details, (
        "the refusal the report carried must survive into history -- "
        "without it a park names no cause")


def test_an_advancing_report_records_that_it_advanced(task_ctx):
    from prism_service.services import resume_actuator as ra

    ctx, task, _ = task_ctx
    advanced = ra._record_report(ctx.task_svc, task.id, "verify_plan",
                                 {"ok": True}, "pass")

    assert advanced is True
    details = str(_rows(ctx, task.id, ra.REPORT_ACTION)[0].details)
    assert "advanced=true" in details
    assert "reason=" not in details, "a clean advance names no refusal"


def test_a_refusal_with_no_reason_still_says_so(task_ctx):
    from prism_service.services import resume_actuator as ra

    ctx, task, _ = task_ctx
    ra._record_report(ctx.task_svc, task.id, "implement_tasks", {"ok": False},
                      "pass")

    details = str(_rows(ctx, task.id, ra.REPORT_ACTION)[0].details)
    assert "flow_report refused and named no reason" in details, (
        "a tooth that computes no reason must SAY it had none, never "
        "leave the row blank")


def test_last_refusal_reads_the_newest_one(task_ctx):
    from prism_service.services import resume_actuator as ra

    ctx, task, _ = task_ctx
    ra._record_report(ctx.task_svc, task.id, "s1", {"ok": False,
                                                    "error": "older"}, "p")
    ra._record_report(ctx.task_svc, task.id, "s1", {"ok": True}, "p")
    ra._record_report(ctx.task_svc, task.id, "s1", {"ok": False,
                                                    "error": "newest"}, "p")

    assert ra._last_refusal(ctx.task_svc, task.id) == "newest"


def test_the_park_names_why_the_last_try_was_refused(task_ctx):
    """The board's blocked_reason is what a person and the next seat read.

    "retry budget spent (3/3) - parked for a human" alone is the string
    that left nine tasks undiagnosable; the park must carry the cause.
    """
    from prism_service.services import resume_actuator as ra

    ctx, task, project = task_ctx
    ra._record_report(ctx.task_svc, task.id, "review_previous_notes",
                      {"ok": False,
                       "error": "premise_grounded: premise_notes is empty"},
                      "pass")

    out = ra._park(project, task.id, attempts=3, max_retries=3)

    assert out["parked"] is True
    reason = str(ctx.task_svc.get(task.id).blocked_reason)
    assert "retry budget spent (3/3)" in reason, "keep the existing summary"
    assert "premise_grounded: premise_notes is empty" in reason, (
        "a parked task must name the refusal that parked it")
