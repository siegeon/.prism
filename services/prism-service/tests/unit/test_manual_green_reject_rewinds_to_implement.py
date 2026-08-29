"""A manual reject at green_gate rewinds to implement_tasks (task 7c7f0f1b).

Before this change, `_reject_gate` (conductor_service.py) rewound a manual
reject to `steps[idx - 1]`, the step before the gate. For green_gate that
step is verify_green_state, which only verifies and reports, so each drive
resubmitted the same branch and the same proof (task 4bb20592: four rejects
on 2026-08-28, last commit 2026-08-23, rework changed no file).

These tests pin the new contract:
- green_gate reject -> implement_tasks (AC-1, AC-6)
- red_gate reject -> write_failing_tests (AC-3)
- plan_gate reject -> verify_plan, unchanged (AC-4)
- the reject reason is verbatim in the next job's instructions (AC-2)
- MAX_AUTO_REWINDS still bounds the loop; override still bypasses it (AC-5)

Fixtures mirror tests/unit/test_gate_reject_rewinds_to_producing_step.py.
"""
import uuid

import pytest


@pytest.fixture()
def make_task():
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(**kwargs):
        project = "green-reject-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        task = ctx.task_svc.create(
            title=kwargs.pop("title", "green reject rewind task"), **kwargs)
        created.append(task.id)
        return ctx, task, project

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


def _gate_id(name: str) -> str:
    from prism_service.models.workflow import WORKFLOW_STEPS

    return next(s["id"] for s in WORKFLOW_STEPS
                if s["type"] == "gate" and s["id"] == name)


def _walk_to_gate(cond, task_id: str, gate_id: str) -> None:
    """Advance a task to gate_id. Each earlier pending gate is cleared with
    a distinct-actor override approve."""
    from prism_service.models.workflow import WORKFLOW_STEPS

    target_idx = next(i for i, s in enumerate(WORKFLOW_STEPS)
                      if s["id"] == gate_id)
    cond._task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk pinning green reject rewind - UNVERIFIED\n"))
    guard = (target_idx + 1) * 3
    cleared = 0
    while guard > 0:
        guard -= 1
        snap = cond._task_svc.get(task_id)
        if snap.workflow_step == gate_id and snap.gate_state == "pending":
            return
        if snap.gate_state == "pending":
            cleared += 1
            cond.gate_decide(
                task_id, action="approve",
                reason="walk_to_gate intermediate; independent re-run",
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}")
            continue
        cond.advance_task(task_id)
    raise AssertionError(f"walk did not reach {gate_id}")


def _reject(cond, task_id, reason, **kw):
    return cond.gate_decide(task_id, action="reject", reason=reason,
                            actor="owner", session_id="owner", **kw)


def test_green_gate_lands_at_implement_tasks(make_task):
    """AC-1: a manual reject at green_gate rewinds to implement_tasks."""
    ctx, task, _project = make_task()
    cond = ctx.conductor_svc
    _walk_to_gate(cond, task.id, _gate_id("green_gate"))

    result = _reject(cond, task.id, "the Trace tab still shows two queues")

    assert result["ok"] is True
    assert result["rewound_to"] == "implement_tasks", result
    assert result["gate_state"] == "none"
    refreshed = ctx.task_svc.get(task.id)
    assert refreshed.workflow_step == "implement_tasks"
    assert refreshed.gate_state == "none"
    assert refreshed.blocked_reason == ""


def test_reason_in_next_job_instructions(make_task):
    """AC-2: the next job carries the reject reason verbatim in
    instructions, not only in the top-level gate_reason field."""
    from prism_service.api import conductor_flow

    ctx, task, _project = make_task()
    cond = ctx.conductor_svc
    _walk_to_gate(cond, task.id, _gate_id("green_gate"))
    sentinel = "REJECT-7c7f0f1b: the Trace tab still shows two queues"

    _reject(cond, task.id, sentinel)

    job = conductor_flow._job(ctx.task_svc.get(task.id))
    assert job is not None
    assert job["step"] == "implement_tasks"
    assert job["gate_reason"] == sentinel
    assert sentinel in job["instructions"], job["instructions"]


def test_red_gate_lands_at_write_failing_tests(make_task):
    """AC-3: a manual reject at red_gate rewinds to write_failing_tests."""
    ctx, task, _project = make_task()
    cond = ctx.conductor_svc
    _walk_to_gate(cond, task.id, _gate_id("red_gate"))

    result = _reject(cond, task.id, "the tests do not fail on baseline")

    assert result["ok"] is True
    assert result["rewound_to"] == "write_failing_tests", result
    refreshed = ctx.task_svc.get(task.id)
    assert refreshed.workflow_step == "write_failing_tests"
    assert refreshed.gate_state == "none"


def test_plan_gate_still_lands_at_verify_plan(make_task):
    """AC-4: the plan_gate target is unchanged (stop_if guard)."""
    ctx, task, _project = make_task()
    cond = ctx.conductor_svc
    _walk_to_gate(cond, task.id, _gate_id("plan_gate"))

    result = _reject(cond, task.id, "the plan names the wrong file")

    assert result["rewound_to"] == "verify_plan", result
    assert ctx.task_svc.get(task.id).workflow_step == "verify_plan"


def test_rewind_budget_still_bounds_green_rejects(make_task):
    """AC-5: MAX_AUTO_REWINDS still bounds green_gate rejects; the fourth
    parks failed; override=True rewinds to implement_tasks."""
    from prism_service.services.conductor_service import MAX_AUTO_REWINDS

    ctx, task, _project = make_task()
    cond = ctx.conductor_svc
    _walk_to_gate(cond, task.id, _gate_id("green_gate"))

    for _ in range(MAX_AUTO_REWINDS):
        cond._task_svc.update(task.id, workflow_step="green_gate",
                              gate_state="pending")
        result = _reject(cond, task.id, "still two queues")
        assert result["rewound_to"] == "implement_tasks", result

    cond._task_svc.update(task.id, workflow_step="green_gate",
                          gate_state="pending")
    result = _reject(cond, task.id, "still two queues")
    assert result["rewound_to"] is None
    assert result["gate_state"] == "failed"
    assert ctx.task_svc.get(task.id).workflow_step == "green_gate"

    result = _reject(cond, task.id, "rework the queue merge", override=True)
    assert result["ok"] is True
    assert result["rewound_to"] == "implement_tasks", result
    assert ctx.task_svc.get(task.id).workflow_step == "implement_tasks"


def test_seat_row_names_target(make_task):
    """AC-6: the audit seat row names the new target."""
    ctx, task, _project = make_task()
    cond = ctx.conductor_svc
    _walk_to_gate(cond, task.id, _gate_id("green_gate"))

    _reject(cond, task.id, "the Trace tab still shows two queues")

    rows = [str(getattr(r, "details", ""))
            for r in (ctx.task_svc.history(task.id) or [])]
    assert any("green_gate -> implement_tasks; manual reject; reason="
               in d for d in rows), rows
