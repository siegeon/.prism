"""A rejected gate must send the task back to its producing step.

Before this fix, gate_decide(action="reject") only set gate_state="failed"
and left task.workflow_step pointing AT the gate. task_runner.eligible_task()
explicitly skips every gate step (pending, passed, OR failed) — gate
adjudication belongs to a distinct seat, not the drive worker — so a
rejected task became a permanent dead end: nothing ever redrove it, and a
human had to hand-nudge it back onto its producing step (as happened with
task 12029f92's rejected plan_gate).

These tests pin the real mechanism: reject rewinds workflow_step to the
step immediately BEFORE the gate in the task's own workflow, clears
gate_state to "none" (the same value advance_task uses for any non-gate
step), and carries the rejection reason forward as gate_reason — which
conductor_flow._job() already splices into every job dict regardless of
step kind, so the redo pass can read WHY it is being redone without a
second fetch. The rewind is bounded by the existing MAX_AUTO_REWINDS
budget (shared with the green_gate machine-refusal auto-rewind) so a
looping reject/redo pair cannot spin forever.
"""
import uuid

import pytest


@pytest.fixture()
def make_task():
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(**kwargs):
        project = "reject-rewind-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        task = ctx.task_svc.create(title=kwargs.pop("title", "reject rewind task"),
                                   **kwargs)
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
    """Advance a task to gate_id, clearing any earlier pending gate with a
    distinct-actor manual override (mirrors test_gate_decide_actor_
    passthrough.py's own helper)."""
    from prism_service.models.workflow import WORKFLOW_STEPS

    target_idx = next(i for i, s in enumerate(WORKFLOW_STEPS)
                       if s["id"] == gate_id)
    cond._task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk pinning reject rewind - UNVERIFIED\n"))
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


def test_reject_rewinds_workflow_step_to_the_producing_step(make_task):
    ctx, task, _project = make_task()
    cond = ctx.conductor_svc
    _walk_to_gate(cond, task.id, _gate_id("plan_gate"))

    result = cond.gate_decide(
        task.id, action="reject",
        reason="plan uses Agent terminology throughout; the system deals "
               "in Bots, not Agents — redo with Bot terminology",
        actor="owner", session_id="owner")

    assert result["ok"] is True
    assert result["rewound_to"] == "verify_plan", (
        f"plan_gate's producing step is verify_plan, got {result!r}")
    assert result["gate_state"] == "none"

    refreshed = ctx.task_svc.get(task.id)
    assert refreshed.workflow_step == "verify_plan"
    assert refreshed.gate_state == "none"
    assert "Bot" in refreshed.gate_reason


def test_rewound_task_is_eligible_for_the_drive_worker_again(make_task):
    """The structural point of the fix: task_runner.eligible_task() skips
    every gate step, so only a workflow_step reset (not a stored gate_state
    flag) actually un-strands the task."""
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    cond = ctx.conductor_svc
    _walk_to_gate(cond, task.id, _gate_id("plan_gate"))
    ctx.task_svc.update(task.id, status="in_progress")

    cond.gate_decide(task.id, action="reject",
                     reason="plan uses Agent terminology, not Bot",
                     actor="owner", session_id="owner")

    assert tr.eligible_task(project) == task.id


def test_reject_rewind_is_bounded_by_the_auto_rewind_budget(make_task):
    from prism_service.services.conductor_service import MAX_AUTO_REWINDS

    ctx, task, _project = make_task()
    cond = ctx.conductor_svc
    _walk_to_gate(cond, task.id, _gate_id("plan_gate"))

    for _ in range(MAX_AUTO_REWINDS):
        cond._task_svc.update(task.id, workflow_step="plan_gate",
                              gate_state="pending")
        result = cond.gate_decide(task.id, action="reject",
                                  reason="still using Agent terminology",
                                  actor="owner", session_id="owner")
        assert result["rewound_to"] == "verify_plan"

    # Budget exhausted: one more reject must NOT rewind — it parks failed,
    # exactly like the pre-fix behaviour, so a human takes over.
    cond._task_svc.update(task.id, workflow_step="plan_gate",
                          gate_state="pending")
    result = cond.gate_decide(task.id, action="reject",
                              reason="still using Agent terminology",
                              actor="owner", session_id="owner")
    assert result["rewound_to"] is None
    assert result["gate_state"] == "failed"

    refreshed = ctx.task_svc.get(task.id)
    assert refreshed.workflow_step == "plan_gate"


def test_failed_gate_reject_override_recovers_via_rewind(make_task):
    """A gate stuck gate_state="failed" from a standing reject (real task
    12029f92: rejected before this rewind mechanism existed) has no
    forward recovery worth taking while the underlying work still needs a
    redo. action="reject" with override=True reuses the same
    producing-step rewind a fresh reject gets, instead of the old
    unconditional "recovery requires action='approve'" refusal."""
    ctx, task, _project = make_task()
    cond = ctx.conductor_svc
    _walk_to_gate(cond, task.id, _gate_id("plan_gate"))
    # exhaust the rewind budget so the gate genuinely parks "failed"
    # (mirrors the real task, rejected repeatedly before ever being fixed)
    from prism_service.services.conductor_service import MAX_AUTO_REWINDS
    for _ in range(MAX_AUTO_REWINDS + 1):
        cond._task_svc.update(task.id, workflow_step="plan_gate",
                              gate_state="pending")
        cond.gate_decide(task.id, action="reject", reason="uses Agent, not Bot",
                         actor="owner", session_id="owner")
    stuck = ctx.task_svc.get(task.id)
    assert stuck.workflow_step == "plan_gate" and stuck.gate_state == "failed"

    plain_reject = cond.gate_decide(task.id, action="reject",
                                    reason="still stuck", actor="owner",
                                    session_id="owner")
    assert plain_reject["ok"] is False, (
        "a plain reject (no override) on an already-failed gate must "
        "still refuse -- override is what signals manual recovery")

    result = cond.gate_decide(task.id, action="reject", override=True,
                              reason="revise with Bot terminology",
                              actor="owner", session_id="owner")
    assert result["ok"] is True
    assert result["rewound_to"] == "verify_plan"
    refreshed = ctx.task_svc.get(task.id)
    assert refreshed.workflow_step == "verify_plan"
    assert refreshed.gate_state == "none"
    assert refreshed.gate_reason == "revise with Bot terminology"
