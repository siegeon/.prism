"""Design packet approved like an evidence package - task c016667f.

plan_gate is scored ONLY by arc_governance.score_plan_coverage today (a pure
text rubric with zero owner-approval concept). This slice gives the design
side the same teeth the evidence side already has: a server-assembled DESIGN
PACKET (plan_diagram + plan_doc + prototype bytes + oracle + likely_misfire)
that must carry a recorded, content-hashed OWNER APPROVAL receipt before any
seat may advance plan_gate.

  AC-1  packet assembles all parts as one addressable object.
  AC-2  packet reports the highest order the feature admits vs. what it has.
  AC-4  plan_gate does not clear on the rubric alone without approval.
  AC-5  the parked reason is non-empty and actionable on every seat.
  AC-6  an explicit owner approval clears the gate.
  AC-7  approval is a receipt: approver + content hash + timestamp.
  AC-8  a render/browser receipt is not accepted as approval.
  AC-9  editing any part after approval reports it stale and re-parks.
  AC-10 a diagram-only non-UI task parks the same way; no ui-tag branch.
"""
from __future__ import annotations

import uuid

import pytest


class _Task:
    """Minimal task stand-in carrying only the fields design_packet reads."""

    def __init__(self, id="t1", oracle="", likely_misfire="", plan_doc="",
                 plan_diagram="", tags=None, workflow_step="plan_gate",
                 gate_state="pending", gate_reason=""):
        self.id = id
        self.oracle = oracle
        self.likely_misfire = likely_misfire
        self.plan_doc = plan_doc
        self.plan_diagram = plan_diagram
        self.tags = tags or []
        self.workflow_step = workflow_step
        self.gate_state = gate_state
        self.gate_reason = gate_reason


@pytest.fixture()
def dp_env(tmp_path, monkeypatch):
    """Isolated PRISM_DATA_DIR so prototype files + approval JSONL never
    touch a real project's data (mirrors the oracle_spec receipt fixtures)."""
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    project = "dp-" + uuid.uuid4().hex[:8]
    return project


def _write_prototype(task_id: str, body: bytes = b"<html>mock</html>"):
    from prism_service import data_dir
    p = data_dir.prototype_file(task_id)
    p.write_bytes(body)
    return p


# ---------------------------------------------------------------------------
# AC-1 - packet assembles all parts as one addressable object
# ---------------------------------------------------------------------------


def test_packet_binds_all_parts_as_one_object(dp_env):
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac1", oracle="oracle text", likely_misfire="misfire",
                plan_doc="AC-1 covered", plan_diagram="stateDiagram-v2\n[*]-->A",
                tags=["ui"])
    _write_prototype(task.id)
    packet = dp.assemble_packet(dp_env, task.id, task=task)
    assert packet["plan_doc"] == "AC-1 covered"
    assert packet["plan_diagram"].startswith("stateDiagram-v2")
    assert packet["oracle"] == "oracle text"
    assert packet["likely_misfire"] == "misfire"
    assert packet["prototype"]["exists"] is True
    assert packet["prototype"]["bytes"] > 0
    assert "content_hash" in packet and packet["content_hash"]


# ---------------------------------------------------------------------------
# AC-2 / AC-10 - highest-order reporting, no ui-tag narrowing on the PARK
# rule itself (order detection may use the tag; approval-required may not)
# ---------------------------------------------------------------------------


def test_order_report_prototype_present_is_top_order(dp_env):
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac2a", tags=["ui"], plan_diagram="flowchart TD\nA-->B")
    _write_prototype(task.id)
    rep = dp.order_report(task, dp.prototype_bytes_for(task.id),
                          task.plan_diagram)
    assert rep["admits"] == "prototype"
    assert rep["actual"] == "prototype"
    assert rep["below_order"] is False


def test_order_report_ui_task_missing_prototype_is_below_order(dp_env):
    """A UI-surface task carrying only a flowchart is reported as below the
    order its feature admits (likely_misfire (a): lower-order substitution)."""
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac2b", tags=["ui"], plan_diagram="flowchart TD\nA-->B")
    rep = dp.order_report(task, dp.prototype_bytes_for(task.id),
                          task.plan_diagram)
    assert rep["admits"] == "prototype"
    assert rep["actual"] != "prototype"
    assert rep["below_order"] is True


def test_order_report_non_ui_task_diagram_only_is_full_order(dp_env):
    """A backend/defect/refactor task's highest admitted order IS the
    diagram - a diagram-only packet is NOT reported below order for it."""
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac2c", tags=[], plan_diagram="stateDiagram-v2\n[*]-->A")
    rep = dp.order_report(task, dp.prototype_bytes_for(task.id),
                          task.plan_diagram)
    assert rep["admits"] == "diagram"
    assert rep["below_order"] is False


# ---------------------------------------------------------------------------
# AC-7 - approval is a RECEIPT: approver + content hash + timestamp
# ---------------------------------------------------------------------------


def test_record_approval_writes_receipt_with_hash_approver_timestamp(dp_env):
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac7", plan_doc="doc", plan_diagram="stateDiagram-v2\n[*]-->A")
    appr = dp.record_approval(dp_env, task.id, task, approver="owner@prism",
                              method="owner_explicit")
    assert appr.approver == "owner@prism"
    assert appr.packet_hash.startswith("sha256:")
    assert appr.approved_at  # non-empty ISO timestamp
    stored = dp.latest_approval(dp_env, task.id)
    assert stored is not None
    assert stored.packet_hash == appr.packet_hash
    assert stored.approver == "owner@prism"


def test_record_approval_requires_an_approver_identity(dp_env):
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac7b", plan_doc="doc")
    with pytest.raises(ValueError):
        dp.record_approval(dp_env, task.id, task, approver="",
                           method="owner_explicit")


# ---------------------------------------------------------------------------
# AC-8 - a render-only / browser receipt is NOT accepted as approval
# ---------------------------------------------------------------------------


def test_record_approval_rejects_browser_render_receipt(dp_env):
    """likely_misfire (e): the browser-oracle 'it loaded' receipt proves the
    pixels exist, not that the owner approved them (the 7cc4f0cf friction)."""
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac8", plan_doc="doc")
    for bad_method in ("browser", "render", "browser_oracle", ""):
        with pytest.raises(ValueError):
            dp.record_approval(dp_env, task.id, task, approver="owner",
                               method=bad_method)
    assert dp.latest_approval(dp_env, task.id) is None


# ---------------------------------------------------------------------------
# AC-4 / AC-6 - approval_status: pending without approval, approved once
# recorded, at the matching content hash.
# ---------------------------------------------------------------------------


def test_approval_status_pending_when_never_approved(dp_env):
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac4", plan_doc="doc", plan_diagram="stateDiagram-v2\n[*]-->A")
    st = dp.approval_status(dp_env, task.id, task)
    assert st["approved"] is False
    assert st["stale"] is False
    assert st["reason"]  # actionable, non-empty


def test_approval_status_approved_after_explicit_approval(dp_env):
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac6", plan_doc="doc", plan_diagram="stateDiagram-v2\n[*]-->A")
    dp.record_approval(dp_env, task.id, task, approver="owner",
                       method="owner_explicit")
    st = dp.approval_status(dp_env, task.id, task)
    assert st["approved"] is True
    assert st["stale"] is False
    assert st["reason"] == ""


# ---------------------------------------------------------------------------
# AC-9 - mutating ANY part after approval reports it STALE (recomputed hash,
# never a stored flag) and re-parks. All three parts, in turn.
# ---------------------------------------------------------------------------


def test_approval_goes_stale_after_plan_doc_edit(dp_env):
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac9a", plan_doc="v1", plan_diagram="stateDiagram-v2\n[*]-->A")
    dp.record_approval(dp_env, task.id, task, approver="owner",
                       method="owner_explicit")
    task.plan_doc = "v2 - rewritten after sign-off"
    st = dp.approval_status(dp_env, task.id, task)
    assert st["approved"] is False
    assert st["stale"] is True
    assert "chang" in st["reason"].lower() or "stale" in st["reason"].lower()


def test_approval_goes_stale_after_plan_diagram_edit(dp_env):
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac9b", plan_doc="doc",
                plan_diagram="stateDiagram-v2\n[*]-->A")
    dp.record_approval(dp_env, task.id, task, approver="owner",
                       method="owner_explicit")
    task.plan_diagram = "stateDiagram-v2\n[*]-->A\nA-->B"
    st = dp.approval_status(dp_env, task.id, task)
    assert st["approved"] is False
    assert st["stale"] is True


def test_approval_goes_stale_after_prototype_replaced(dp_env):
    from prism_service.services import design_packet as dp
    task = _Task(id="t-ac9c", plan_doc="doc",
                plan_diagram="stateDiagram-v2\n[*]-->A")
    _write_prototype(task.id, b"<html>v1</html>")
    dp.record_approval(dp_env, task.id, task, approver="owner",
                       method="owner_explicit")
    _write_prototype(task.id, b"<html>v2 - redesigned</html>")
    st = dp.approval_status(dp_env, task.id, task)
    assert st["approved"] is False
    assert st["stale"] is True


def test_approvals_are_append_only_not_overwritten(dp_env):
    """Mirrors oracle_spec._receipts_path / append_receipt (append-only
    JSONL) - a re-approval adds a row, it never rewrites prior history."""
    from prism_service.services import design_packet as dp
    task = _Task(id="t-append", plan_doc="doc")
    dp.record_approval(dp_env, task.id, task, approver="owner",
                       method="owner_explicit")
    task.plan_doc = "doc v2"
    dp.record_approval(dp_env, task.id, task, approver="owner",
                       method="owner_explicit")
    history = dp.read_approvals(dp_env, task.id)
    assert len(history) == 2


# ---------------------------------------------------------------------------
# AC-4 / AC-5 / AC-6 - the three DECIDING seats consult packet approval, not
# the rubric alone. Mirrors _conductor() in test_gate_adjudicator_seat.py.
# ---------------------------------------------------------------------------


def _real_task(tmp_path, plan_doc="AC-1 covered", plan_diagram="stateDiagram-v2\n[*]-->A",
               tags=None, oracle="oracle text"):
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="design packet probe", oracle=oracle,
                        proof_type="demo", tags=tags or [])
    task_svc.update(t.id, workflow_step="plan_gate", gate_state="pending",
                    plan_doc=plan_doc, plan_diagram=plan_diagram)
    return task_svc, task_svc.get(t.id)


def _conductor(tmp_path, task_svc, project):
    from prism_service.services.conductor_service import ConductorService
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = project
    return cond


def test_autoclear_parks_plan_gate_pending_without_approval(dp_env, tmp_path,
                                                             monkeypatch):
    """AC-4: even a rubric-verified plan stays PENDING at plan_gate when no
    design-packet approval is recorded - the rubric is no longer enough."""
    from prism_service.api import conductor_flow as cf
    task_svc, task = _real_task(tmp_path, tags=["ui"])
    _write_prototype(task.id)
    cond = _conductor(tmp_path, task_svc, dp_env)
    monkeypatch.setattr(cond, "_verify_gate",
                        lambda t, step_id, proof_type=None:
                        {"verified": True, "reason": "stub rubric green",
                         "verifier": None, "validation": "plan_coverage"})
    res = cf._autoclear_machine_gate(cond, task.id)
    assert res is None, res
    after = task_svc.get(task.id)
    assert after.workflow_step == "plan_gate"
    assert after.gate_state == "pending"
    assert after.gate_reason, "AC-5: reason must be non-empty, never blank"
    assert after.gate_state != "failed"


def test_autoclear_clears_plan_gate_after_explicit_approval(dp_env, tmp_path,
                                                             monkeypatch):
    """AC-6: recording an explicit owner approval lets the SAME seat advance
    the gate on its next run."""
    from prism_service.api import conductor_flow as cf
    from prism_service.services import design_packet as dp
    task_svc, task = _real_task(tmp_path, tags=["ui"])
    _write_prototype(task.id)
    dp.record_approval(dp_env, task.id, task, approver="owner",
                       method="owner_explicit")
    cond = _conductor(tmp_path, task_svc, dp_env)
    monkeypatch.setattr(cond, "_verify_gate",
                        lambda t, step_id, proof_type=None:
                        {"verified": True, "reason": "stub rubric green",
                         "verifier": None, "validation": "plan_coverage"})
    res = cf._autoclear_machine_gate(cond, task.id)
    assert res is not None and res.get("ok") is True, res
    assert task_svc.get(task.id).workflow_step != "plan_gate"


def test_resweep_adjudicate_rubric_gate_withholds_without_approval(
        dp_env, tmp_path, monkeypatch):
    """AC-4/AC-11 (supersedes test_pending_rubric_gate_is_resweepable in
    test_gate_adjudicator_seat.py:410-426): the re-sweep adjudicator seat
    must NOT approve a rubric-green plan_gate lacking a design approval."""
    task_svc, task = _real_task(tmp_path, tags=["ui"])
    _write_prototype(task.id)
    cond = _conductor(tmp_path, task_svc, dp_env)
    monkeypatch.setattr(
        cond, "_verify_rubric_gate",
        lambda t, validation: {"verified": True, "reason": "stub green",
                               "verifier": None, "validation": validation})
    res = cond.adjudicate_rubric_gate(task.id)
    assert res is None, res
    assert task_svc.get(task.id).workflow_step == "plan_gate"


def test_autoclear_parks_diagram_only_non_ui_task_too(dp_env, tmp_path,
                                                       monkeypatch):
    """AC-10: a backend/defect/refactor task with NO ui tag and NO prototype
    file parks the same way for approval - the requirement is unconditional,
    never narrowed to ui-tagged tasks (owner 2026-08-03 supersession)."""
    from prism_service.api import conductor_flow as cf
    task_svc, task = _real_task(tmp_path, tags=[])  # no "ui" tag, no prototype
    cond = _conductor(tmp_path, task_svc, dp_env)
    monkeypatch.setattr(cond, "_verify_gate",
                        lambda t, step_id, proof_type=None:
                        {"verified": True, "reason": "stub rubric green",
                         "verifier": None, "validation": "plan_coverage"})
    res = cf._autoclear_machine_gate(cond, task.id)
    assert res is None, res
    after = task_svc.get(task.id)
    assert after.workflow_step == "plan_gate"
    assert after.gate_state == "pending"
    assert after.gate_reason


# ---------------------------------------------------------------------------
# AC-5, third seat - gate_adjudicator._pending_decline_reason must NOT return
# "" for a rubric-verified plan_gate lacking a design approval (today it does:
# gate_adjudicator.py:66-69 `if check.get("verified") is True: return ""`).
# ---------------------------------------------------------------------------


def test_pending_decline_reason_names_missing_approval(dp_env, tmp_path,
                                                        monkeypatch):
    from prism_service.services import gate_adjudicator as ga
    task_svc, task = _real_task(tmp_path, tags=["ui"])
    _write_prototype(task.id)
    cond = _conductor(tmp_path, task_svc, dp_env)
    monkeypatch.setattr(
        cond, "_verify_rubric_gate",
        lambda t, validation: {"verified": True, "reason": "stub green",
                               "verifier": None, "validation": validation})
    reason = ga._pending_decline_reason(cond, task, "plan_gate", dp_env)
    assert reason, "must not be empty when approval is missing"
    assert "approv" in reason.lower()
