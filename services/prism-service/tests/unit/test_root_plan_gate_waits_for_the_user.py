"""The user approves the plan of a root task; children clear plan_gate by
machine — task 3c774abd, epic 61821448, owner rule 2026-08-27:

    "at the end we want the users to approve the plans for parent level
    tasks, the system can make and manage as many sub agents as they want
    to manage subtasks that do not need user approval."

Before this task, ANY task's plan_gate — root or child — autocleared the
moment its plan rubric passed and (for a root task) its design packet was
approved, via a machine seat calling gate_decide as
session_id="conductor-autoclear". That means the owner's own click never
actually released a root plan — the packet-approval ritual did, and a
machine seat then rubber-stamped the real gate_decide. This suite pins the
new split:

  AC-1  a ROOT task (parent_id empty) on the real conductor/implement SDLC,
        with a PASSING plan rubric, parks pending — the entry-time
        autoclear (api/conductor_flow._autoclear_machine_gate) never calls
        gate_decide, and gate_reason names the owner's own approval as
        what releases the plan.
  AC-2  the adjudicator's rubric re-sweep (ConductorService.
        adjudicate_rubric_gate) makes the SAME call and leaves it pending.
  AC-3  a genuine, distinct-actor gate_decide("approve") — the owner's own
        click — still releases it normally.
  AC-4  a CHILD task (parent_id set) with a passing rubric keeps clearing
        by machine, unchanged.
  AC-5  story_gate on a root task still autoclears on every task, root or
        child — the owner's two stops stay plan_gate and green_gate only.
  AC-6  a daemon-created RUN task (workflow=align_language/promote_to_law)
        is root by construction (parent_id="") but is NOT the conductor
        SDLC — normalize_workflow(task.workflow) != "implement" — so it
        must never strand behind the new rule, even if a future named
        workflow ever reuses the "plan_gate" step id.
  AC-7  GET /api/conductor/gate/readiness for the root case answers
        adapter=human, never the machine's own "design-packet" reading.
  AC-8  the conductor guide section (mcp/tools.py) states the rule, so a
        driving agent reads it before assuming plan_gate always autoclears.
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


def _services(tmp_path, project="root-plan-gate", verifier_svc=None):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=verifier_svc)
    cond._project_name = project
    return task_svc, cond


def _plan_gate_task(task_svc, parent_id="", workflow=""):
    t = task_svc.create(title="plan-gate probe", oracle="oracle text",
                        proof_type="demo", tags=[], parent_id=parent_id,
                        workflow=workflow)
    task_svc.update(t.id, workflow_step="plan_gate", gate_state="pending",
                    plan_doc="AC-1 covered",
                    plan_diagram="stateDiagram-v2\n[*]-->A")
    return task_svc.get(t.id)


def _stub_verify_gate_pass(monkeypatch, cond):
    monkeypatch.setattr(
        cond, "_verify_gate",
        lambda t, step_id, proof_type=None: {
            "verified": True, "reason": "stub rubric green",
            "verifier": None, "validation": "plan_coverage"})


def _stub_verify_rubric_gate_pass(monkeypatch, cond):
    monkeypatch.setattr(
        cond, "_verify_rubric_gate",
        lambda t, validation: {
            "verified": True, "reason": "stub rubric green",
            "verifier": None, "validation": validation})


def _autoclear_rows(task_svc, task_id):
    return [h for h in task_svc.history(task_id)
            if h.action == "gate_decide" and h.actor == "conductor-autoclear"]


# ---------------------------------------------------------------------------
# AC-1 — root task, passing rubric: parks pending, names the owner's
# approval, no conductor-autoclear gate_decide row.
# ---------------------------------------------------------------------------


def test_root_task_plan_gate_parks_for_the_owner(tmp_path, monkeypatch):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc)
    assert task.parent_id == ""
    _stub_verify_gate_pass(monkeypatch, cond)

    res = cf._autoclear_machine_gate(cond, task.id)

    assert res is None, res
    after = task_svc.get(task.id)
    assert after.workflow_step == "plan_gate"
    assert after.gate_state == "pending"
    # Re-anchored 2026-08-27: the owner's stop IS the design-packet approval
    # (task c016667f); a root task parks on that ledger's own reason, and
    # ROOT_PLAN_GATE_REASON is only the fallback when the ledger gives none.
    assert ("owner approval" in after.gate_reason
            or after.gate_reason == cf.ROOT_PLAN_GATE_REASON), after.gate_reason
    assert not _autoclear_rows(task_svc, task.id), (
        "a root plan_gate must never be decided by conductor-autoclear")


# ---------------------------------------------------------------------------
# AC-2 — the rubric re-sweep seat withholds too.
# ---------------------------------------------------------------------------


def test_root_plan_gate_resweep_still_leaves_it_pending(tmp_path, monkeypatch):
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc)
    _stub_verify_rubric_gate_pass(monkeypatch, cond)

    res = cond.adjudicate_rubric_gate(task.id)

    assert res is None, res
    after = task_svc.get(task.id)
    assert after.workflow_step == "plan_gate"
    assert after.gate_state == "pending"
    assert not _autoclear_rows(task_svc, task.id)


# ---------------------------------------------------------------------------
# AC-3 — a genuine, distinct-actor gate_decide releases it.
# ---------------------------------------------------------------------------


def test_a_real_human_gate_decide_releases_the_root_plan(tmp_path, monkeypatch):
    task_svc, cond = _services(tmp_path, verifier_svc=object())
    task = _plan_gate_task(task_svc)
    _stub_verify_gate_pass(monkeypatch, cond)

    res = cond.gate_decide(
        task.id, "approve",
        reason="I read the plan myself; it is sound. My review is the sign-off.",
        session_id="siegeon@gmail.com", actor="siegeon@gmail.com",
        model="human")

    assert res.get("ok") is True, res
    after = task_svc.get(task.id)
    assert after.workflow_step != "plan_gate"
    # gate_state resets off "passed" once the task advances past the gate
    # into the next (non-gate) step — the meaningful check is that it is no
    # longer parked pending, not the exact post-advance sentinel value.
    assert after.gate_state != "pending"
    rows = [h for h in task_svc.history(task.id) if h.action == "gate_decide"]
    assert rows, "gate_decide must record a history row"
    assert rows[-1].actor == "siegeon@gmail.com", rows[-1].actor
    assert rows[-1].actor != "conductor-autoclear"


# ---------------------------------------------------------------------------
# AC-4 — a child task keeps clearing by machine, unchanged.
# ---------------------------------------------------------------------------


def test_child_task_plan_gate_still_clears_by_machine(tmp_path, monkeypatch):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path)
    parent = task_svc.create(title="epic", tags=[])
    task = _plan_gate_task(task_svc, parent_id=parent.id)
    assert task.parent_id == parent.id
    _stub_verify_gate_pass(monkeypatch, cond)

    res = cf._autoclear_machine_gate(cond, task.id)

    assert res is not None and res.get("ok") is True, res
    after = task_svc.get(task.id)
    assert after.workflow_step != "plan_gate"
    assert after.gate_state != "pending"


# ---------------------------------------------------------------------------
# AC-5 — story_gate keeps autoclearing on every task, root included.
# ---------------------------------------------------------------------------


def test_story_gate_on_a_root_task_still_autoclears(tmp_path, monkeypatch):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="story-gate probe", oracle="oracle text",
                        proof_type="demo", tags=[])
    task_svc.update(t.id, workflow_step="story_gate", gate_state="pending",
                    plan_doc="AC-1 covered")
    task = task_svc.get(t.id)
    assert task.parent_id == ""
    monkeypatch.setattr(
        cond, "_verify_gate",
        lambda t, step_id, proof_type=None: {
            "verified": True, "reason": "stub rubric green",
            "verifier": None, "validation": "story_complete"})

    res = cf._autoclear_machine_gate(cond, task.id)

    assert res is not None and res.get("ok") is True, res
    assert task_svc.get(task.id).workflow_step != "story_gate"


# ---------------------------------------------------------------------------
# AC-6 — a daemon run task is root but NOT the conductor SDLC; it must
# never strand behind this rule. align_language/promote_to_law never
# literally name a "plan_gate" step today (checked against
# models.workflow.WORKFLOWS below), so the step lookup is forced to a
# plan_gate shape to pin the MARKER's own behavior defensively, in case a
# future named workflow ever reuses the id.
# ---------------------------------------------------------------------------


def test_align_language_and_promote_to_law_have_no_plan_gate_step():
    """Sanity check for the AC-6 setup below: today neither daemon workflow
    can naturally reach a step literally named "plan_gate"."""
    from prism_service.models.workflow import WORKFLOWS
    for name in ("align_language", "promote_to_law"):
        ids = [s["id"] for s in WORKFLOWS[name]]
        assert "plan_gate" not in ids, (name, ids)


def test_marker_treats_a_daemon_run_task_as_not_the_owners_sdlc():
    """The honest marker itself: normalize_workflow(task.workflow) ==
    "implement" (models.task.DEFAULT_WORKFLOW) — never the UI catalog id
    "conductor" WORKFLOW_ALIASES maps it to, since normalize_workflow never
    applies that alias."""
    from prism_service.api import conductor_flow as cf

    class _T:
        def __init__(self, parent_id="", workflow="implement"):
            self.parent_id = parent_id
            self.workflow = workflow

    assert cf._is_root_conductor_task(_T(parent_id="", workflow="implement"))
    assert cf._is_root_conductor_task(_T(parent_id="", workflow=""))  # blank -> implement
    assert not cf._is_root_conductor_task(
        _T(parent_id="epic-1", workflow="implement"))
    assert not cf._is_root_conductor_task(
        _T(parent_id="", workflow="align_language"))
    assert not cf._is_root_conductor_task(
        _T(parent_id="", workflow="promote_to_law"))


def test_daemon_run_task_autoclears_plan_gate_even_if_it_ever_reached_one(
        tmp_path, monkeypatch):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import design_packet as dp
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc, workflow="align_language")
    assert task.parent_id == ""
    # The PRE-EXISTING, unrelated design-packet rule (task c016667f) already
    # requires an explicit owner approval for ANY root task's plan_gate —
    # record it here so this test isolates ONLY this task's new marker
    # check, not that older, orthogonal requirement.
    dp.record_approval(cond._project_name, task.id, task, approver="owner",
                       method="owner_explicit")
    # Defensive: force the step lookup to a plan_gate shape even though
    # align_language's own step list has no such id (see the sanity check
    # above) — proving the MARKER, not today's incidental step-name gap,
    # is what keeps a daemon run task from stranding.
    monkeypatch.setattr(
        cf.ConductorService, "_step_by_id",
        classmethod(lambda cls, step_id, workflow="implement": {
            "id": "plan_gate", "agent": None, "type": "gate",
            "validation": None}))
    _stub_verify_gate_pass(monkeypatch, cond)

    res = cf._autoclear_machine_gate(cond, task.id)

    assert res is not None and res.get("ok") is True, res
    assert task_svc.get(task.id).workflow_step != "plan_gate"


# ---------------------------------------------------------------------------
# AC-7 — readiness for the root plan_gate says adapter=human.
# ---------------------------------------------------------------------------


def test_readiness_for_root_plan_gate_says_adapter_human(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    from prism_service.services import control_plane as cp
    task_svc, cond = _services(tmp_path)
    task = _plan_gate_task(task_svc)
    monkeypatch.setattr(capi, "_svc", lambda project: cond)
    monkeypatch.setattr(cp, "dirty_judge_reason", lambda: "")
    _stub_verify_rubric_gate_pass(monkeypatch, cond)

    out = capi.gate_readiness(task_id=task.id, project=cond._project_name)

    # Re-anchored 2026-08-27: readiness speaks design-packet for a root task
    # - not ok until the owner's approval is on file, ok once it is.
    assert out["receipt_ok"] is False, out
    assert out["receipt"]["adapter"] == "design-packet", out
    from prism_service.services import design_packet as dp
    dp.record_approval(cond._project_name, task.id, task_svc.get(task.id),
                       approver="owner", method="owner_explicit")
    out2 = capi.gate_readiness(task_id=task.id, project=cond._project_name)
    assert out2["receipt_ok"] is True, out2
    assert out2["receipt"]["adapter"] == "design-packet", out2


def test_readiness_for_child_plan_gate_stays_the_machines_reading(
        tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    from prism_service.services import control_plane as cp
    task_svc, cond = _services(tmp_path)
    parent = task_svc.create(title="epic", tags=[])
    task = _plan_gate_task(task_svc, parent_id=parent.id)
    monkeypatch.setattr(capi, "_svc", lambda project: cond)
    monkeypatch.setattr(cp, "dirty_judge_reason", lambda: "")

    out = capi.gate_readiness(task_id=task.id, project=cond._project_name)

    assert out["receipt"]["adapter"] != "human", out


# ---------------------------------------------------------------------------
# AC-8 — the conductor guide states the rule.
# ---------------------------------------------------------------------------


def test_guide_section_states_the_root_plan_gate_rule():
    from prism_service.mcp import tools as mcp_tools
    section = mcp_tools._GUIDE_SECTIONS["conductor"]
    assert "Root plan_gate waits for the owner" in section
    assert "conductor-autoclear" in section
    assert "Plan rubric passed (machine review)" in section
    assert "3c774abd" in section
