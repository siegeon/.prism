"""A gate never parks for a person while its own rubric fails — task
3feaf956.

On 2026-08-28 task 12029f92 reached plan_gate five times. Each time the
player ran the real scorers from arc_governance with the live rubrics and
both failed: score_story_complete returned missing required sections
Summary and Requirements, and score_plan_coverage returned that the plan
covers none of AC-1 to AC-10. A person was asked to judge a design packet
the machine already knew was incomplete.

  AC(a)  a ROOT task at plan_gate with a plan_doc that genuinely fails
         score_plan_coverage is REWOUND to verify_plan (the producing
         step), gate_state becomes "none", and gate_reason carries the
         scorer's own reason -- never left pending for the owner.
  AC(b)  the next job (api/conductor_flow._job) built for that rewound
         task carries the scorer reason in its instructions, so a driving
         agent self-diagnoses without a second fetch.
  AC(c)  a ROOT task at plan_gate whose rubric PASSES still parks pending
         for the owner -- unchanged (this ticket adds a REWIND path, it
         does not touch the pass path at all).
  AC(d)  a CHILD task whose rubric passes still autoclears by machine,
         unchanged.
  AC(e)  a CHILD task whose rubric FAILS is also rewound, not stranded --
         the "never park while known-failing" rule applies regardless of
         who would eventually decide the gate.
  AC(f)  story_gate applies the identical rule: a story missing required
         sections rewinds to draft_story with the scorer reason.
  AC(g)  stop_if: a SCORER ERROR (the rubric function itself raised) still
         just PARKS with the error -- never rewound, so a broken scorer
         cannot loop a task forever. The rubric is scored exactly ONCE per
         park decision (this ticket's own likely_misfire: re-scoring at
         park doubles the gate latency).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _services(tmp_path, project="gate-never-parks"):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = project
    return task_svc, cond


# No memory_svc is attached above, so arc_governance.load_principles never
# runs and score_plan_coverage's own misfire guard ("no architecture
# principles seeded — conformance cannot be scored") fires reliably: a
# REAL rubric failure, not a stub standing in for one.
FAILING_PLAN_DOC = "## Plan\nAC-1 through AC-10 - nothing seeded yet.\n"
FAILING_PLAN_DIAGRAM = "flowchart TD\n  api --> domain\n"

MISSING_SECTIONS_STORY = (
    "# Story: incomplete\n\n## Acceptance Criteria\n"
    "- AC-1: something happens — oracle: pytest tests/test_x.py\n")


def _plan_gate_task(task_svc, parent_id=""):
    t = task_svc.create(title="plan-gate rubric probe", oracle="oracle text",
                        proof_type="demo", tags=[], parent_id=parent_id)
    task_svc.update(t.id, workflow_step="plan_gate", gate_state="pending",
                    plan_doc=FAILING_PLAN_DOC,
                    plan_diagram=FAILING_PLAN_DIAGRAM)
    return task_svc.get(t.id)


def _story_gate_task(task_svc, parent_id=""):
    t = task_svc.create(title="story-gate rubric probe", oracle="oracle text",
                        proof_type="demo", tags=[], parent_id=parent_id)
    task_svc.update(t.id, workflow_step="story_gate", gate_state="pending",
                    plan_doc=MISSING_SECTIONS_STORY)
    return task_svc.get(t.id)


def _autoclear_rows(task_svc, task_id):
    return [h for h in task_svc.history(task_id)
            if h.action == "gate_decide" and h.actor == "conductor-autoclear"]


def _rewind_rows(task_svc, task_id):
    return [h for h in task_svc.history(task_id) if h.action == "auto_rewind"]


# ---------------------------------------------------------------------------
# AC(a) — a root task at plan_gate with a genuinely failing rubric rewinds.
# ---------------------------------------------------------------------------


def test_root_plan_gate_with_failing_rubric_rewinds_to_verify_plan(tmp_path):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc)
    assert task.parent_id == ""

    res = cf._autoclear_machine_gate(cond, task.id)

    assert res is not None and res.get("ok") is True, res
    assert res.get("rewound_to") == "verify_plan", res
    after = task_svc.get(task.id)
    assert after.workflow_step == "verify_plan", after.workflow_step
    assert after.gate_state == "none", after.gate_state
    assert "no architecture principles seeded" in after.gate_reason, \
        after.gate_reason
    assert not _autoclear_rows(task_svc, task.id), (
        "a failing rubric must never be machine-APPROVED")
    assert _rewind_rows(task_svc, task.id), (
        "the bounce must be recorded as an auto_rewind row")


# ---------------------------------------------------------------------------
# AC(b) — the next job carries the scorer reason in its instructions.
# ---------------------------------------------------------------------------


def test_next_job_instructions_carry_the_scorer_reason(tmp_path):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc)

    cf._autoclear_machine_gate(cond, task.id)

    after = task_svc.get(task.id)
    job = cf._job(after)
    assert job is not None
    assert job["step"] == "verify_plan"
    assert "no architecture principles seeded" in job["instructions"], \
        job["instructions"]


# ---------------------------------------------------------------------------
# AC(c) — a root task whose rubric PASSES still parks pending for the
# owner. This ticket adds a REWIND path; it must not touch the pass path.
# ---------------------------------------------------------------------------


def test_root_plan_gate_with_passing_rubric_still_parks_for_the_owner(
        tmp_path, monkeypatch):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc)
    monkeypatch.setattr(
        cond, "_verify_gate",
        lambda t, step_id, proof_type=None: {
            "verified": True, "reason": "stub rubric green",
            "verifier": {"ok": True}, "validation": "plan_coverage"})

    res = cf._autoclear_machine_gate(cond, task.id)

    assert res is None, res
    after = task_svc.get(task.id)
    assert after.workflow_step == "plan_gate"
    assert after.gate_state == "pending"
    assert not _autoclear_rows(task_svc, task.id)
    assert not _rewind_rows(task_svc, task.id)


# ---------------------------------------------------------------------------
# AC(d) — a child task whose rubric passes still autoclears by machine.
# ---------------------------------------------------------------------------


def test_child_plan_gate_with_passing_rubric_still_autoclears(
        tmp_path, monkeypatch):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path)
    parent = task_svc.create(title="epic", tags=[])
    task = _plan_gate_task(task_svc, parent_id=parent.id)
    monkeypatch.setattr(
        cond, "_verify_gate",
        lambda t, step_id, proof_type=None: {
            "verified": True, "reason": "stub rubric green",
            "verifier": {"ok": True}, "validation": "plan_coverage"})

    res = cf._autoclear_machine_gate(cond, task.id)

    assert res is not None and res.get("ok") is True, res
    after = task_svc.get(task.id)
    assert after.workflow_step != "plan_gate"
    assert after.gate_state != "pending"
    rows = [h for h in task_svc.history(task.id) if h.action == "gate_decide"]
    assert rows and "auto-clear" in rows[-1].details, rows


# ---------------------------------------------------------------------------
# AC(e) — a child task whose rubric FAILS is rewound too, not stranded.
# ---------------------------------------------------------------------------


def test_child_plan_gate_with_failing_rubric_also_rewinds(tmp_path):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path)
    parent = task_svc.create(title="epic", tags=[])
    task = _plan_gate_task(task_svc, parent_id=parent.id)

    res = cf._autoclear_machine_gate(cond, task.id)

    assert res is not None and res.get("ok") is True, res
    assert res.get("rewound_to") == "verify_plan", res
    after = task_svc.get(task.id)
    assert after.workflow_step == "verify_plan"
    assert after.gate_state == "none"
    assert not _autoclear_rows(task_svc, task.id)


# ---------------------------------------------------------------------------
# AC(f) — story_gate applies the identical rule.
# ---------------------------------------------------------------------------


def test_story_gate_with_failing_rubric_rewinds_to_draft_story(tmp_path):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path)
    task = _story_gate_task(task_svc)

    res = cf._autoclear_machine_gate(cond, task.id)

    assert res is not None and res.get("ok") is True, res
    assert res.get("rewound_to") == "draft_story", res
    after = task_svc.get(task.id)
    assert after.workflow_step == "draft_story"
    assert after.gate_state == "none"
    assert "missing required section" in after.gate_reason, after.gate_reason
    assert not _autoclear_rows(task_svc, task.id)


# ---------------------------------------------------------------------------
# AC(g) — stop_if: a SCORER ERROR parks, never rewinds/loops, and the
# rubric is scored exactly once per park decision.
# ---------------------------------------------------------------------------


def test_scorer_error_parks_instead_of_rewinding(tmp_path, monkeypatch):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc)
    # The EXACT shape ConductorService._verify_rubric_gate returns when the
    # scorer itself raises (conductor_service.py): verifier is None,
    # distinguishing a genuine SCORE from a broken scorer.
    monkeypatch.setattr(
        cond, "_verify_gate",
        lambda t, step_id, proof_type=None: {
            "verified": False,
            "reason": "rubric scoring raised ValueError: boom",
            "verifier": None, "validation": "plan_coverage"})

    res = cf._autoclear_machine_gate(cond, task.id)

    assert res is None, res
    after = task_svc.get(task.id)
    assert after.workflow_step == "plan_gate", (
        "a scorer error must park the task, never rewind/loop it")
    assert after.gate_state == "pending"
    assert "rubric scoring raised" in after.gate_reason, after.gate_reason
    assert not _rewind_rows(task_svc, task.id)


def test_rubric_scored_exactly_once_per_park_decision(tmp_path, monkeypatch):
    """The likely_misfire this ticket names: 'the pre-park check runs the
    rubric twice per park and doubles the gate latency.' Count real calls
    into score_plan_coverage across one _autoclear_machine_gate pass."""
    from prism_service.api import conductor_flow as cf
    from prism_service.services import arc_governance as gov
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc)
    calls = []
    real = gov.score_plan_coverage

    def _counting(evidence, rubric, principles):
        calls.append(1)
        return real(evidence, rubric, principles)

    monkeypatch.setattr(gov, "score_plan_coverage", _counting)

    cf._autoclear_machine_gate(cond, task.id)

    assert len(calls) == 1, (
        f"score_plan_coverage ran {len(calls)} times for one park decision")


# ---------------------------------------------------------------------------
# AC(h) — stop_if: "Verification fails twice." The SAME rubric failure
# recurring at the same gate PARKS instead of rewinding a second time, so
# a producing step that resubmits unchanged content (e.g. a headless
# worker/fixture that never actually fixes anything) cannot bounce a task
# between the gate and its producing step forever
# (test_worker_contract_enforced.py and test_conductor_work_honest_green.py
# hit exactly this against fixtures with no real story/plan content).
# ---------------------------------------------------------------------------


def test_same_rubric_failure_twice_parks_instead_of_rewinding_again(
        tmp_path):
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc)

    from prism_service.api import conductor_flow as cf
    first = cf._autoclear_machine_gate(cond, task.id)
    assert first is not None and first.get("ok") is True, first
    assert first.get("rewound_to") == "verify_plan", first
    after_first = task_svc.get(task.id)
    assert after_first.workflow_step == "verify_plan"
    assert after_first.gate_state == "none"

    # The producing step is "re-reported" with the SAME broken content
    # (nothing changed) and lands back at plan_gate — the shape a headless
    # drive loop produces when nobody fixes the plan in between.
    task_svc.update(task.id, workflow_step="plan_gate", gate_state="pending")

    second = cf._autoclear_machine_gate(cond, task.id)

    assert second is None, (
        "a repeat of the identical rubric failure must PARK, not rewind "
        f"again: {second}")
    after_second = task_svc.get(task.id)
    assert after_second.workflow_step == "plan_gate", after_second.workflow_step
    assert after_second.gate_state == "pending"
    assert len(_rewind_rows(task_svc, task.id)) == 1, (
        "exactly one rewind should have happened, not a second")


def test_a_different_rubric_failure_still_rewinds_a_second_time(tmp_path):
    """A CHANGED failure reason (real, if partial, progress) still earns
    another rewind — only an IDENTICAL repeat parks."""
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc)

    from prism_service.api import conductor_flow as cf
    first = cf._autoclear_machine_gate(cond, task.id)
    assert first is not None and first.get("ok") is True, first

    task_svc.update(task.id, workflow_step="plan_gate", gate_state="pending")

    real_verify_gate = cond._verify_gate

    def _once_different(t, step_id, proof_type=None):
        res = real_verify_gate(t, step_id, proof_type=proof_type)
        return {**res, "reason": "a different, new rubric complaint"}

    cond._verify_gate = _once_different
    try:
        second = cf._autoclear_machine_gate(cond, task.id)
    finally:
        cond._verify_gate = real_verify_gate

    assert second is not None and second.get("ok") is True, second
    assert second.get("rewound_to") == "verify_plan", second
    assert len(_rewind_rows(task_svc, task.id)) == 2
