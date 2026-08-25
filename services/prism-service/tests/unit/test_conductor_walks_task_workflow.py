"""Task 6f22d0ad: the conductor walks a task's OWN workflow steps.

Prior to this task, ConductorService._workflow_steps/_step_index/
_step_by_id/advance_task (and api/conductor_flow.py's _job, which calls
_step_by_id) always read the global models.workflow.WORKFLOW_STEPS,
documented "No per-task override" -- so a task.workflow="triage" task
still walked the 10-step implement SDLC (see test_triage_workflow.py's
SCOPE NOTE, which predates this fix). This pins the per-task lookup: the
step LIST now resolves via models.workflow.steps_for(task.workflow), and
a default/blank/unknown workflow is BYTE-FOR-BYTE unchanged.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent


def _services(tmp_path):
    """Same fixture shape as test_conductor_v2_state_machine.py's own
    _services(tmp_path): no verifier_service attached, so gate_decide
    trusts the caller's approve directly (the "Legacy [1/4] behavior"
    branch) -- exactly like every story_gate/plan_gate test in that file."""
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"),
        enable_engine=False,
        task_svc=task_svc,
    )
    return task_svc, cond


# ----------------------------------------------------------------------
# (1) _workflow_steps resolves per workflow; default is untouched
# ----------------------------------------------------------------------

def test_workflow_steps_resolves_triage_and_leaves_the_default_untouched():
    from prism_service.models.workflow import TRIAGE_STEPS, WORKFLOW_STEPS
    from prism_service.services.conductor_service import ConductorService

    triage_ids = [s["id"] for s in ConductorService._workflow_steps("triage")]
    assert triage_ids == [s["id"] for s in TRIAGE_STEPS]

    for value in ("implement", "", "carrier-pigeon"):
        ids = [s["id"] for s in ConductorService._workflow_steps(value)]
        assert ids == [s["id"] for s in WORKFLOW_STEPS], value

    # No-arg call (every pre-existing external caller) is unaffected too.
    assert ([s["id"] for s in ConductorService._workflow_steps()]
            == [s["id"] for s in WORKFLOW_STEPS])


# ----------------------------------------------------------------------
# (2) advance_task on a triage task walks intake -> classify -> decide ->
#     done, and never lands on draft_story.
# ----------------------------------------------------------------------

def test_advance_task_walks_a_triage_task_to_done_never_draft_story(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="A triaged inbox item")
    task_svc.update(t.id, workflow="triage")

    visited = []
    r = cond.advance_task(t.id)  # "" -> intake
    assert r["ok"] is True, r
    visited.append(r["to_step"])

    r = cond.advance_task(t.id)  # intake -> classify
    assert r["ok"] is True, r
    visited.append(r["to_step"])

    r = cond.advance_task(t.id)  # classify -> decide (gate, now pending)
    assert r["ok"] is True, r
    visited.append(r["to_step"])
    refreshed = task_svc.get(t.id)
    assert refreshed.gate_state == "pending"

    decided = cond.gate_decide(
        t.id, action="approve",
        reason="triage: bucket=Open, needs a reply from support")
    assert decided["ok"] is True, decided
    visited.append(decided["to_step"])  # decide -> done

    assert visited == ["intake", "classify", "decide", "done"]
    assert "draft_story" not in visited

    refreshed = task_svc.get(t.id)
    assert refreshed.workflow_step == "done"

    # The terminal step refuses to advance further, same shape as the
    # implement workflow's own final-step refusal.
    over = cond.advance_task(t.id)
    assert over["ok"] is False
    assert "final" in over["reason"]


# ----------------------------------------------------------------------
# (3) conductor_flow._job for a triage task at classify
# ----------------------------------------------------------------------

def test_job_for_triage_classify_names_the_four_buckets(tmp_path):
    task_svc, _cond = _services(tmp_path)
    t = task_svc.create(title="A triage item")
    task_svc.update(t.id, workflow="triage", workflow_step="classify")
    refreshed = task_svc.get(t.id)

    from prism_service.api import conductor_flow

    job = conductor_flow._job(refreshed)
    assert job is not None
    assert job["step"] == "classify"
    for bucket in ("Open", "Monitoring", "Resolved", "Dropped"):
        assert bucket in job["instructions"], job["instructions"]


# ----------------------------------------------------------------------
# (4) A default task's first transitions are byte-for-byte unchanged
# ----------------------------------------------------------------------

def test_default_tasks_first_transitions_are_byte_for_byte_unchanged(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="Default workflow walk")
    # premise_grounded is check_at_step on review_previous_notes -> draft_story
    # (unrelated to this task); seed it so this raw walk isn't refused there.
    task_svc.update(t.id, premise_notes=(
        "## Premises\n- fixture walk exercising raw state-machine "
        "transitions, not a real premise claim - UNVERIFIED\n"))

    # Pinned literally (task 6f22d0ad): the pre-existing implement sequence,
    # byte-for-byte -- a change here means the DEFAULT workflow's own step
    # order silently moved, which this task must never do.
    expected = ["review_previous_notes", "draft_story", "story_gate"]
    visited = []
    for _ in expected:
        result = cond.advance_task(t.id)
        assert result["ok"] is True, result
        visited.append(result["to_step"])

    assert visited == expected
