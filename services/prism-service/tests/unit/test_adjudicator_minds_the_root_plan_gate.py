"""The adjudicator minds the root plan gate - task 594f9a58.

Owner 2026-08-30: "we moved from here. if we have to escalate p90
certainty or whatever then you can block on the gate, but the ontology and
adjudicator is to mind the gates, we made it a game."

BEFORE this task, a ROOT task's plan_gate could clear ONLY through the
owner's own POST /api/conductor/design-packet/approve
(method="owner_explicit") - both machine seats
(api/conductor_flow._autoclear_machine_gate and
services/gate_adjudicator.sweep_once's plan_gate re-sweep) parked
unconditionally the moment design_packet.approval_status(...).approved was
False, with no certainty measure at all. So no seat, however distinct,
could ever decide a root plan_gate.

THIS SUITE pins the fix:

  AC-1  design_packet.plan_gate_certainty returns a REAL, non-constant
        score - a "rich" packet and a "thin" packet score differently, and
        their per-signal breakdown differs too.
  AC-2  a root task at plan_gate with a passing rubric and a certainty
        score at/above the threshold clears through the adjudicator seat
        (design_packet.adjudicate_root_plan_gate) with no human action;
        the gate_decide history row names the seat and the certainty.
  AC-3  a root task below the threshold stays parked pending, and
        gate_reason names a concrete, specific thing to judge - never
        empty, never the generic ledger placeholder alone.
  AC-4  design_packet.record_approval still accepts ONLY
        method="owner_explicit" - the certainty-approve path never widens
        _ALLOWED_METHODS and never appends to the approvals ledger, even
        though it DID approve the gate.
  AC-5  the threshold is configurable via
        PRISM_PLAN_GATE_CERTAINTY_THRESHOLD (default 0.90, degrading to
        0.90 on a garbage value), and both seats -
        api.conductor_flow._autoclear_machine_gate (entry-time) and
        design_packet.adjudicate_root_plan_gate (consulted by
        gate_adjudicator's re-sweep) - agree on the same verdict for the
        same fixture task because both call the same function.

Note on scope (task 594f9a58's own likely_misfire guard, re-read against
control_plane.POLICY_FILES): services/prism_service/services/
conductor_service.py IS a pinned policy file, so this change deliberately
never edits it - ConductorService.adjudicate_rubric_gate is untouched. The
certainty decision lives entirely in design_packet.py (a non-policy
service module) and is wired in from two non-policy call sites:
api/conductor_flow.py's entry-time seat and services/gate_adjudicator.py's
re-sweep dispatch.
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


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def dp_env(tmp_path, monkeypatch):
    """Isolated PRISM_DATA_DIR so prototype files + approval JSONL never
    touch a real project's data (mirrors test_design_packet_plan_gate.py's
    own dp_env fixture)."""
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    return "adj-plan-gate-" + uuid.uuid4().hex[:8]


class _Task:
    """Minimal task stand-in carrying only the fields design_packet reads -
    same shape as test_design_packet_plan_gate.py's own stand-in, extended
    with parent_id/workflow/id so _root_conductor_plan_gate can read them."""

    def __init__(self, id="t1", oracle="", likely_misfire="", plan_doc="",
                plan_diagram="", tags=None, workflow_step="plan_gate",
                gate_state="pending", gate_reason="", parent_id="",
                workflow="implement"):
        self.id = id
        self.oracle = oracle
        self.likely_misfire = likely_misfire
        self.plan_doc = plan_doc
        self.plan_diagram = plan_diagram
        self.tags = tags or []
        self.workflow_step = workflow_step
        self.gate_state = gate_state
        self.gate_reason = gate_reason
        self.parent_id = parent_id
        self.workflow = workflow


_LONG_ORACLE = ("A root task's plan_gate is decided honestly, with a "
                "clear, observable check a human could run themselves.")
_LONG_MISFIRE = ("The certainty could be a constant that always clears, "
                 "silently removing the owner's veto without telling them.")
assert len(_LONG_ORACLE) >= 40 and len(_LONG_MISFIRE) >= 40


def _words(n: int) -> str:
    return " ".join(["word"] * n)


def _rich_task(**overrides) -> _Task:
    kwargs = dict(
        id="rich-" + uuid.uuid4().hex[:8],
        oracle=_LONG_ORACLE, likely_misfire=_LONG_MISFIRE,
        plan_doc="AC-1: covered. " + _words(160),
        plan_diagram="flowchart TD\nA-->B\nB-->C\nC-->D",
        tags=[], parent_id="", workflow="implement")
    kwargs.update(overrides)
    return _Task(**kwargs)


def _thin_task(**overrides) -> _Task:
    kwargs = dict(
        id="thin-" + uuid.uuid4().hex[:8],
        oracle="short", likely_misfire="short",
        plan_doc="AC-1 covered", plan_diagram="",
        tags=[], parent_id="", workflow="implement")
    kwargs.update(overrides)
    return _Task(**kwargs)


def _mid_task(**overrides) -> _Task:
    """Scores comfortably below the 0.90 default but above a lowered
    threshold - completeness ~0.5, oracle 1.0, diagram 0.5 (one edge),
    scope 1.0 -> average ~0.75."""
    kwargs = dict(
        id="mid-" + uuid.uuid4().hex[:8],
        oracle=_LONG_ORACLE, likely_misfire=_LONG_MISFIRE,
        plan_doc="AC-1: covered. " + _words(74),
        plan_diagram="flowchart TD\nA-->B",
        tags=[], parent_id="", workflow="implement")
    kwargs.update(overrides)
    return _Task(**kwargs)


def _services(tmp_path, project="root-plan-gate-adj", verifier_svc=None):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=verifier_svc)
    cond._project_name = project
    return task_svc, cond


def _real_root_task(task_svc, template: _Task):
    t = task_svc.create(
        title="plan-gate certainty probe", oracle=template.oracle,
        likely_misfire=template.likely_misfire, proof_type="test",
        tags=template.tags, parent_id=template.parent_id,
        workflow=template.workflow)
    task_svc.update(t.id, workflow_step="plan_gate", gate_state="pending",
                    plan_doc=template.plan_doc,
                    plan_diagram=template.plan_diagram)
    return task_svc.get(t.id)


def _stub_verify_rubric_gate_pass(monkeypatch, cond):
    monkeypatch.setattr(
        cond, "_verify_rubric_gate",
        lambda t, validation: {
            "verified": True, "reason": "stub rubric green",
            "verifier": None, "validation": validation})


def _stub_verify_gate_pass(monkeypatch, cond):
    monkeypatch.setattr(
        cond, "_verify_gate",
        lambda t, step_id, proof_type=None: {
            "verified": True, "reason": "stub rubric green",
            "verifier": None, "validation": "plan_coverage"})


def _adjudicator_rows(task_svc, task_id):
    return [h for h in task_svc.history(task_id)
            if h.action == "gate_decide"]


# ---------------------------------------------------------------------------
# AC-1 - a real, non-constant certainty score.
# ---------------------------------------------------------------------------


def test_certainty_is_real_and_differs_between_rich_and_thin_packets(dp_env):
    from prism_service.services import design_packet as dp
    rich = _rich_task()
    thin = _thin_task()

    rich_c = dp.plan_gate_certainty(dp_env, rich.id, rich)
    thin_c = dp.plan_gate_certainty(dp_env, thin.id, thin)

    assert rich_c["score"] > thin_c["score"], (rich_c, thin_c)
    assert rich_c["signals"] != thin_c["signals"]
    assert rich_c["score"] >= dp.certainty_threshold()
    assert thin_c["score"] < dp.certainty_threshold()
    # every signal is independently present, none hardcoded away
    for key in ("plan_completeness", "oracle_quality", "diagram_quality",
               "scope_alignment"):
        assert key in rich_c["signals"] and key in thin_c["signals"]
    assert thin_c["reasons"], "a low-scoring packet must name why"


# ---------------------------------------------------------------------------
# AC-2 - the adjudicator approves a root plan_gate on certainty alone.
# ---------------------------------------------------------------------------


def test_adjudicator_approves_root_plan_gate_on_certainty(tmp_path, dp_env,
                                                           monkeypatch):
    from prism_service.services import design_packet as dp
    from prism_service.services.conductor_service import ADJUDICATOR_SEAT
    task_svc, cond = _services(tmp_path, project=dp_env)
    task = _real_root_task(task_svc, _rich_task())
    assert task.parent_id == ""
    _stub_verify_rubric_gate_pass(monkeypatch, cond)

    res = dp.adjudicate_root_plan_gate(cond, task.id, task, dp_env)

    assert res is not None and res.get("ok") is True, res
    after = task_svc.get(task.id)
    assert after.workflow_step != "plan_gate"
    assert after.gate_state != "pending"
    rows = _adjudicator_rows(task_svc, task.id)
    assert rows, "gate_decide must record a history row"
    last = rows[-1]
    assert last.actor == ADJUDICATOR_SEAT, last.actor
    assert "certainty=" in (last.details or last.reason or ""), last


# ---------------------------------------------------------------------------
# AC-3 - below-threshold packet stays parked with a concrete reason.
# ---------------------------------------------------------------------------


def test_thin_packet_parks_with_a_concrete_escalation_reason(tmp_path,
                                                              dp_env,
                                                              monkeypatch):
    from prism_service.services import design_packet as dp
    task_svc, cond = _services(tmp_path, project=dp_env)
    task = _real_root_task(task_svc, _thin_task())
    _stub_verify_rubric_gate_pass(monkeypatch, cond)

    res = dp.adjudicate_root_plan_gate(cond, task.id, task, dp_env)

    assert res is not None and res.get("ok") is not True, res
    after = task_svc.get(task.id)
    assert after.workflow_step == "plan_gate"
    assert after.gate_state == "pending"
    assert after.gate_reason, "a parked root plan_gate must name a reason"
    certainty = dp.plan_gate_certainty(dp_env, task.id, after)
    assert any(r in after.gate_reason for r in certainty["reasons"]), (
        after.gate_reason, certainty["reasons"])
    assert not _adjudicator_rows(task_svc, task.id), (
        "a parked gate must not carry a gate_decide row")


# ---------------------------------------------------------------------------
# AC-4 - record_approval stays owner_explicit-only; the ledger stays empty
# even after a certainty-approve.
# ---------------------------------------------------------------------------


def test_record_approval_still_refuses_a_machine_method(dp_env):
    from prism_service.services import design_packet as dp
    task = _rich_task()
    with pytest.raises(ValueError):
        dp.record_approval(dp_env, task.id, task, approver="conductor-adjudicator",
                           method="conductor-adjudicator")
    with pytest.raises(ValueError):
        dp.record_approval(dp_env, task.id, task, approver="conductor-adjudicator",
                           method="adjudicator_certainty")


def test_certainty_approve_never_writes_the_approvals_ledger(tmp_path,
                                                              dp_env,
                                                              monkeypatch):
    from prism_service.services import design_packet as dp
    task_svc, cond = _services(tmp_path, project=dp_env)
    task = _real_root_task(task_svc, _rich_task())
    _stub_verify_rubric_gate_pass(monkeypatch, cond)

    res = dp.adjudicate_root_plan_gate(cond, task.id, task, dp_env)

    assert res is not None and res.get("ok") is True, res
    assert dp.read_approvals(dp_env, task.id) == [], (
        "a machine certainty-approve must never append an owner-approval "
        "receipt")


def test_adjudicate_root_plan_gate_ignores_a_child_task(tmp_path, dp_env,
                                                        monkeypatch):
    """A child task's plan_gate is not this seat's remit at all - it must
    return None so the caller falls through to the ordinary rubric
    autoclear, completely unchanged."""
    from prism_service.services import design_packet as dp
    task_svc, cond = _services(tmp_path, project=dp_env)
    parent = task_svc.create(title="epic", tags=[])
    task = _real_root_task(task_svc, _rich_task(parent_id=parent.id))
    assert task.parent_id == parent.id
    _stub_verify_rubric_gate_pass(monkeypatch, cond)

    res = dp.adjudicate_root_plan_gate(cond, task.id, task, dp_env)

    assert res is None, res
    assert task_svc.get(task.id).gate_state == "pending"


# ---------------------------------------------------------------------------
# AC-5 - configurable threshold; both seats agree.
# ---------------------------------------------------------------------------


def test_certainty_threshold_configurable_and_degrades_to_default(monkeypatch):
    from prism_service.services import design_packet as dp
    monkeypatch.delenv("PRISM_PLAN_GATE_CERTAINTY_THRESHOLD", raising=False)
    assert dp.certainty_threshold() == pytest.approx(0.90)

    monkeypatch.setenv("PRISM_PLAN_GATE_CERTAINTY_THRESHOLD", "0.5")
    assert dp.certainty_threshold() == pytest.approx(0.5)

    monkeypatch.setenv("PRISM_PLAN_GATE_CERTAINTY_THRESHOLD", "nope")
    assert dp.certainty_threshold() == pytest.approx(0.90)

    monkeypatch.setenv("PRISM_PLAN_GATE_CERTAINTY_THRESHOLD", "5")
    assert dp.certainty_threshold() == pytest.approx(0.90)


def test_lowering_the_threshold_clears_a_mid_range_packet(tmp_path, dp_env,
                                                           monkeypatch):
    from prism_service.services import design_packet as dp
    task_svc, cond = _services(tmp_path, project=dp_env)
    task = _real_root_task(task_svc, _mid_task())
    _stub_verify_rubric_gate_pass(monkeypatch, cond)

    parked = dp.adjudicate_root_plan_gate(cond, task.id, task, dp_env)
    assert parked is not None and parked.get("ok") is not True, parked
    assert task_svc.get(task.id).gate_state == "pending"

    monkeypatch.setenv("PRISM_PLAN_GATE_CERTAINTY_THRESHOLD", "0.5")
    approved = dp.adjudicate_root_plan_gate(cond, task.id, task, dp_env)
    assert approved is not None and approved.get("ok") is True, approved
    assert task_svc.get(task.id).gate_state != "pending"


def test_entry_time_seat_and_resweep_seat_agree(tmp_path, dp_env, monkeypatch):
    """api.conductor_flow._autoclear_machine_gate (entry-time) and
    design_packet.adjudicate_root_plan_gate (what gate_adjudicator's
    re-sweep consults) must reach the identical verdict for the identical
    fixture - both go through the one certainty function."""
    from prism_service.api import conductor_flow as cf
    from prism_service.services import design_packet as dp

    # -- rich fixture: both approve --
    task_svc, cond = _services(tmp_path, project=dp_env)
    rich = _real_root_task(task_svc, _rich_task())
    _stub_verify_gate_pass(monkeypatch, cond)
    _stub_verify_rubric_gate_pass(monkeypatch, cond)
    from prism_service.services import plan_gate_checks as _pgc
    monkeypatch.setattr(_pgc, "refusal", lambda task, project: "")

    flow_res = cf._autoclear_machine_gate(cond, rich.id)
    assert flow_res is not None and flow_res.get("ok") is True, flow_res

    task_svc2, cond2 = _services(tmp_path, project=dp_env + "-2")
    rich2 = _real_root_task(task_svc2, _rich_task())
    _stub_verify_rubric_gate_pass(monkeypatch, cond2)
    seat_res = dp.adjudicate_root_plan_gate(cond2, rich2.id, rich2,
                                            dp_env + "-2")
    assert seat_res is not None and seat_res.get("ok") is True, seat_res

    # -- thin fixture: both park, same reason --
    task_svc3, cond3 = _services(tmp_path, project=dp_env + "-3")
    thin = _real_root_task(task_svc3, _thin_task())
    _stub_verify_gate_pass(monkeypatch, cond3)
    _stub_verify_rubric_gate_pass(monkeypatch, cond3)

    flow_park = cf._autoclear_machine_gate(cond3, thin.id)
    assert flow_park is None, flow_park
    flow_after = task_svc3.get(thin.id)
    assert flow_after.gate_state == "pending"
    assert flow_after.gate_reason

    task_svc4, cond4 = _services(tmp_path, project=dp_env + "-4")
    thin2 = _real_root_task(task_svc4, _thin_task())
    _stub_verify_rubric_gate_pass(monkeypatch, cond4)
    seat_park = dp.adjudicate_root_plan_gate(cond4, thin2.id, thin2,
                                             dp_env + "-4")
    assert seat_park is not None and seat_park.get("ok") is not True
    seat_after = task_svc4.get(thin2.id)
    assert seat_after.gate_reason == flow_after.gate_reason, (
        seat_after.gate_reason, flow_after.gate_reason)
