"""A plain gate (validation=None) reads ready for the human — task 085ee5ff.

DEFECT: GET /api/conductor/gate/readiness had no branch for a gate step
declared with validation=None (TRIAGE_STEPS "decide", PROMOTE_TO_LAW_STEPS
"review"). It fell through to the green_gate oracle tooth and answered
"green_gate: oracle not evidenced - no EvidenceReceipt on file" for a gate
that can NEVER have a receipt by design. The web card's Approve button read
`disabled={busy || readiness?.receipt_ok !== true}`, so the human's only
offered exit was Override.

  AC-1  a task on workflow "triage" at workflow_step "decide" reads
        receipt_ok True and manual_review True — the Approve button is
        enabled, not disabled.
  AC-2  the reason text does NOT contain "green_gate" — it is not falsely
        claiming a machine gate refusal for a plain human gate.
  AC-3  the reason text says this gate has no machine rubric and the human's
        review is the sign-off.
  AC-4  anti-regression: a task at workflow_step "green_gate" still gets the
        oracle refusal (unchanged behaviour — this is where the regression
        guard lives).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def test_plain_gate_triage_decide_reads_ready(tmp_path, monkeypatch):
    """AC-1: triage workflow, decide step reads receipt_ok True."""
    from prism_service.api import conductor as capi

    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="triage task")
    task_svc.update(t.id, workflow="triage", workflow_step="decide",
                    gate_state="pending")

    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=t.id, project="default")

    assert out["receipt_ok"] is True, (
        f"plain gate should be ready to approve; got receipt_ok={out['receipt_ok']}")
    assert out.get("manual_review") is True, (
        f"plain gate should have manual_review=True; got {out}")


def test_plain_gate_reason_does_not_mention_green_gate(tmp_path, monkeypatch):
    """AC-2: the reason text does NOT contain 'green_gate'."""
    from prism_service.api import conductor as capi

    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="triage task")
    task_svc.update(t.id, workflow="triage", workflow_step="decide",
                    gate_state="pending")

    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=t.id, project="default")
    reason = str((out.get("receipt") or {}).get("reason") or "")

    assert "green_gate" not in reason, (
        f"plain gate must not claim green_gate refusal; got reason: {reason}")


def test_plain_gate_reason_says_sign_off(tmp_path, monkeypatch):
    """AC-3: reason text says human review is the sign-off."""
    from prism_service.api import conductor as capi

    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="triage task")
    task_svc.update(t.id, workflow="triage", workflow_step="decide",
                    gate_state="pending")

    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=t.id, project="default")
    reason = str((out.get("receipt") or {}).get("reason") or "")

    assert "no machine rubric" in reason, (
        f"reason should mention no machine rubric; got: {reason}")
    assert "review is the sign-off" in reason, (
        f"reason should say review is sign-off; got: {reason}")


def test_plain_gate_promote_to_law_review_reads_ready(tmp_path, monkeypatch):
    """plain gate readiness works for promote_to_law workflow 'review' step."""
    from prism_service.api import conductor as capi

    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="promote to law task")
    task_svc.update(t.id, workflow="promote_to_law", workflow_step="review",
                    gate_state="pending")

    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=t.id, project="default")

    assert out["receipt_ok"] is True, (
        f"promote_to_law review gate should be ready; got {out}")
    assert out.get("manual_review") is True, out


def test_green_gate_still_gets_oracle_refusal_regression_guard(
        tmp_path, monkeypatch):
    """AC-4: anti-regression — green_gate still rejects when oracle is missing."""
    from prism_service.api import conductor as capi

    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="test task")
    task_svc.update(t.id, workflow="implement", workflow_step="green_gate",
                    gate_state="pending")

    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=t.id, project="default")

    # green_gate should NOT read ready when there's no oracle/receipt
    # (this is the unchanged, regression-guard behaviour)
    assert out["receipt_ok"] is not True, (
        f"green_gate without evidence should not read ready; got {out}")
