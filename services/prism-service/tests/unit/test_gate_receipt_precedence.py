"""Oracle-receipt precedence at the green gate (68e5c699 interim fix).

The trusted-runner EvidenceReceipt is the green gate's DESIGNED deciding
authority (inverted-flow #2). The in-daemon shell verifier is known
env-fragile (false REDs from sanitized-env skips / empty diff scope), so:

  - a FRESH PASSING receipt releases a green_gate approve even when the
    shell verifier disagrees (both verdicts recorded), and
  - without such a receipt, a failing shell verifier still refuses —
    the precedence NEVER weakens the gate when no receipt exists.

Owner-driven: 'that approve click should enable the model working the
task queue to move forward' (2026-07-14).
"""

from __future__ import annotations

from types import SimpleNamespace


def _services(tmp_path):
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def _to_green_gate(task_svc, cond, task_id):
    """Advance a fresh task all the way to green_gate (gate pending),
    trust-caller approving each earlier gate (no verifier attached).

    task 3928b7ac (issue #222 continued): review_previous_notes'
    premise_grounded check is now unconditional on its OWN dedicated
    task.premise_notes field (never completion_proof) — a fixture walk
    unrelated to premise content must seed it once, up front, or it never
    leaves review_previous_notes.
    """
    task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising green_gate receipt "
        "precedence, not a real premise claim - UNVERIFIED\n"))
    for _ in range(30):
        t = task_svc.get(task_id)
        if t.workflow_step == "green_gate" and t.gate_state == "pending":
            return
        if t.gate_state == "pending":
            cond.gate_decide(task_id, "approve", reason="setup walk")
        else:
            cond.advance_task(task_id)
    raise AssertionError("never reached green_gate")


class _FailingVerifier:
    """Shell verifier stub: green_full always refuses (env-fragile stand-in)."""

    def verify(self, *a, **k):  # pragma: no cover - shape only
        return {"status": "fail", "tier0": "fail"}


def _wire_failing_green(cond):
    cond._verifier_svc = _FailingVerifier()
    cond._verify_gate = lambda task, gate_step_id, proof_type=None: {
        "verified": False,
        "reason": "verifier rejected: fail: 0/2 pass (env-fragile stub)",
        "verifier": {"status": "fail"},
        "validation": "green_full",
    }


_GREEN_PROOF = (
    "full suite green: 1447 passed in 150s — pytest tests -q; oracle walked "
    "live on http://127.0.0.1:8888 (screenshots on file); refutes misfire: "
    "old rows render."
)


def test_fresh_passing_receipt_releases_green_gate(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="receipt precedence pass",
                        oracle="http://127.0.0.1:8888/tasks serves the board")
    task_svc.update(t.id, completion_proof=_GREEN_PROOF)
    _to_green_gate(task_svc, cond, t.id)
    _wire_failing_green(cond)
    receipt = SimpleNamespace(job_id="r1-fresh", adapter="http_probe",
                              passed=True, status="passed", tree_sha="t1",
                              spec_hash="s1", policy_hash="", ended_at="",
                              reason="all assertions held")
    cond._oracle_receipt_refusal = lambda task, override, reason: ("", receipt)

    res = cond.gate_decide(t.id, "approve",
                           reason="owner approve on live evidence")
    assert res.get("ok") is True, res
    live = task_svc.get(t.id)
    assert live.gate_state == "passed", live.gate_state


def test_no_receipt_keeps_the_refusal(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="receipt precedence refuse",
                        oracle="http://127.0.0.1:8888/tasks serves the board")
    task_svc.update(t.id, completion_proof=_GREEN_PROOF)
    _to_green_gate(task_svc, cond, t.id)
    _wire_failing_green(cond)
    cond._oracle_receipt_refusal = lambda task, override, reason: (
        "green_gate: oracle not evidenced — no EvidenceReceipt on file", None)

    res = cond.gate_decide(t.id, "approve", reason="should refuse")
    assert res.get("ok") is False, res
    live = task_svc.get(t.id)
    assert live.gate_state == "failed", live.gate_state


def test_recovery_recheck_honors_fresh_receipt(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="receipt precedence recovery",
                        oracle="http://127.0.0.1:8888/tasks serves the board")
    task_svc.update(t.id, completion_proof=_GREEN_PROOF)
    _to_green_gate(task_svc, cond, t.id)
    _wire_failing_green(cond)
    # First approve without a receipt: refused -> gate_state failed.
    cond._oracle_receipt_refusal = lambda task, override, reason: (
        "no EvidenceReceipt on file", None)
    first = cond.gate_decide(t.id, "approve", reason="first try")
    assert first.get("ok") is False
    assert task_svc.get(t.id).gate_state == "failed"
    # Evidence fixed (fresh passing receipt): the RECOVERY approve (no
    # override) now releases despite the still-failing shell verifier.
    receipt = SimpleNamespace(job_id="r2-fresh", adapter="http_probe",
                              passed=True, status="passed", tree_sha="t2",
                              spec_hash="s2", policy_hash="", ended_at="",
                              reason="all assertions held")
    cond._oracle_receipt_refusal = lambda task, override, reason: ("", receipt)
    second = cond.gate_decide(t.id, "approve", reason="evidence fixed")
    assert second.get("ok") is True, second
    assert task_svc.get(t.id).gate_state == "passed"
