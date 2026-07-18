"""task 8f48f9bb — a PENDING gate the machine adjudicator can't auto-clear
must SURFACE its already-computed reason on task.gate_reason, so a driving
agent self-diagnoses instead of stalling to ping a human. The fix lives in
gate_adjudicator (a non-policy file); these pin the two helpers it adds.
"""
import types

from prism_service.services import gate_adjudicator as ga


class _Task:
    def __init__(self, id="t1", workflow_step="plan_gate", gate_state="pending",
                 gate_reason=""):
        self.id = id
        self.workflow_step = workflow_step
        self.gate_state = gate_state
        self.gate_reason = gate_reason
        self.plan_doc = ""
        self.plan_diagram = ""


class _FakeSvc:
    """ConductorService stand-in — only the seams the reason helper reads."""
    def __init__(self, verdict):
        self._verdict = verdict

    def _validation_for_gate(self, step):
        return {"story_gate": "story_complete",
                "plan_gate": "plan_coverage"}.get(step)

    def _verify_rubric_gate(self, task, validation):
        return self._verdict


class _FakeTaskSvc:
    def __init__(self, task):
        self._task = task
        self.updates = []

    def get(self, tid):
        return self._task

    def update(self, tid, **kw):
        self.updates.append((tid, kw))
        for k, v in kw.items():
            setattr(self._task, k, v)
        return self._task


def test_ac1_story_plan_decline_reason_is_returned():
    """AC-1: a story/plan rubric verdict of verified=False yields the verdict's
    specific reason string (not "")."""
    svc = _FakeSvc({"verified": False,
                    "reason": "plan_coverage: plan_diagram is missing"})
    r = ga._pending_decline_reason(svc, _Task(workflow_step="plan_gate"),
                                   "plan_gate", "prism")
    assert r == "plan_coverage: plan_diagram is missing"


def test_ac2_red_decline_reason_comes_from_receipt(monkeypatch):
    """AC-2: a red gate's decline reason is the latest non-passing
    EvidenceReceipt's reason."""
    rc = types.SimpleNamespace(
        passed=False, reason="red not demonstrated (rc=4, wanted rc==1)")
    from prism_service.services import oracle_spec as osp
    monkeypatch.setattr(osp, "latest_receipt", lambda project, tid: rc)
    r = ga._pending_decline_reason(_FakeSvc({}),
                                   _Task(id="t1", workflow_step="red_gate"),
                                   "red_gate", "prism")
    assert r == "red not demonstrated (rc=4, wanted rc==1)"


def test_ac3_write_stamps_gate_reason_and_keeps_pending():
    """AC-3: the writer stamps gate_reason on a pending gate AND never advances
    gate_state (stays 'pending')."""
    t = _Task(gate_state="pending", gate_reason="")
    ctx = types.SimpleNamespace(task_svc=_FakeTaskSvc(t))
    ga._write_pending_reason(ctx, t, "plan_coverage: plan_diagram is missing")
    assert t.gate_reason == "plan_coverage: plan_diagram is missing"
    assert t.gate_state == "pending"
    assert ctx.task_svc.updates  # a write actually happened


def test_ac4_approvable_gate_has_no_decline_reason():
    """AC-4: when the rubric verdict IS verified, there is no decline reason to
    write, so the approval path's own reason is never overwritten."""
    svc = _FakeSvc({"verified": True,
                    "reason": "plan_coverage: 5 AC id(s) covered"})
    r = ga._pending_decline_reason(svc, _Task(workflow_step="plan_gate"),
                                   "plan_gate", "prism")
    assert r == ""


def test_ac5_decided_gate_reason_not_clobbered():
    """AC-5: a gate already decided (passed/failed) is never re-stamped."""
    t = _Task(gate_state="passed", gate_reason="verified (green_receipt): ok")
    ctx = types.SimpleNamespace(task_svc=_FakeTaskSvc(t))
    ga._write_pending_reason(ctx, t, "some new decline reason")
    assert t.gate_reason == "verified (green_receipt): ok"
    assert not ctx.task_svc.updates  # no write on a decided gate
