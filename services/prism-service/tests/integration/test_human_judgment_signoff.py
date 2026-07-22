"""A human can actually SIGN OFF a visual/demo gate (owner 2026-07-19).

The machine adjudicator leaves a human-judgment gate pending (test_gate_
adjudicator_seat AC-2/AC-2b). This pins the OTHER half: a person's plain
Approve (distinct actor, NO override) signs it off — the machine oracle +
test-shaped verifier teeth are skipped for a demo/visual gate — while the
visual-evidence requirement (the artifact tooth) still holds. Reuses the
adjudicator-seat harness.
"""
from tests.integration.test_gate_adjudicator_seat import (  # noqa: F401
    pinned_world, _gated_task, _conductor,
)


def test_demo_gate_plain_approve_with_evidence_signs_off(pinned_world, tmp_path):
    task_svc, task = _gated_task(
        tmp_path, oracle="the customer can read the page",
        proof_type="demo", verify=[])
    # A demo sign-off carries its VISUAL evidence (owner: default to visual).
    task_svc.update(task.id, completion_proof=(
        "![shot](/api/tasks/x/evidence/sign-off.png) screenshot at "
        "127.0.0.1:8888"))
    cond = _conductor(tmp_path, task_svc)
    res = cond.gate_decide(task.id, "approve", reason="looks right to me",
                           session_id="owner-sess", actor="owner")
    assert res.get("ok") is True and res.get("gate_state") == "passed", res
    after = task_svc.get(task.id)
    assert after.status == "done"


def test_demo_gate_plain_approve_without_evidence_is_refused(pinned_world,
                                                            tmp_path):
    # No visual artifact -> the artifact tooth refuses even a human approve
    # (the sign-off still needs the evidence captured).
    task_svc, task = _gated_task(
        tmp_path, oracle="the customer can read the page",
        proof_type="demo", verify=[])
    task_svc.update(task.id, completion_proof="looks green to me")
    cond = _conductor(tmp_path, task_svc)
    res = cond.gate_decide(task.id, "approve", reason="ok",
                           session_id="owner-sess", actor="owner")
    assert res.get("ok") is False
