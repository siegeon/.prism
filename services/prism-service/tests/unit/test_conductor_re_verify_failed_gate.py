"""Honest re-verify path for a latched-failed gate (task 19e31e88).

A green_gate/red_gate that latched ``gate_state='failed'`` (often from a
transient/env artifact during the verifier run) could previously ONLY be
cleared with ``approve + override=True`` — override bookkeeping for what
should be a clean re-verify. These tests pin the new ``re_verify`` flag:
``approve`` on a FAILED gate with ``re_verify=True`` (NOT override) resets
the gate to pending, re-runs the VerifierService fresh, and releases ONLY
if the verifier now passes — a normal verifier-passed release with NO
override actor and NO manual-override audit row. A genuinely red slice
still cannot re-arm green via re_verify (the verifier still fails it).

Backend-only tests with a scripted FakeVerifier (no shelling out).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class FakeVerifier:
    """Returns scripted results in order (``.script``) so a first FAIL then
    a later PASS can simulate a transient artifact clearing on re-verify."""

    def __init__(self, result: Optional[dict] = None) -> None:
        self.calls: list[dict] = []
        self.next_result: Optional[dict] = result
        self.script: list[dict] = []

    def run(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        if self.script:
            return self.script.pop(0)
        assert self.next_result is not None, "FakeVerifier has no result queued"
        return self.next_result


_FAIL = {"status": "fail", "tier0": "fail", "tier1": "not-run",
         "tier2": "skipped", "summary": "2 tests failing (transient)"}
_PASS = {"status": "pass", "tier0": "pass", "tier1": "pass",
         "tier2": "skipped", "summary": "all green"}

# green_gate artifact tooth (proof_type unset -> test shape) wants a runner
# + a pass signal in the decision reason.
_GREEN_REASON = "re-verify: pytest -q -> 320 passed, 0 failed (tree now green)"


def _services(tmp_path, verifier: Optional[FakeVerifier] = None):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"),
        enable_engine=False,
        task_svc=task_svc,
        verifier_svc=verifier,
    )
    return task_svc, cond


def _workflow():
    from prism_service.models.workflow import WORKFLOW_STEPS

    return WORKFLOW_STEPS


def _green_gate_id() -> str:
    return "green_gate"


def _walk_to_gate(cond, task_id: str, gate_id: str) -> None:
    """Advance to gate_id, clearing earlier pending gates with a distinct-actor
    override approve carrying a real failing-test trace (artifact tooth)."""
    steps = _workflow()
    target_idx = next(i for i, s in enumerate(steps) if s["id"] == gate_id)
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
                reason=("walk_to_gate intermediate; independent re-run: "
                        "pytest -q -> 1 failed"),
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}")
            continue
        cond.advance_task(task_id)


def _manual_override_rows(task_svc, task_id: str) -> list:
    return [r for r in task_svc.history(task_id)
            if r.action == "gate_decide" and r.actor == "manual-override"]


# ----------------------------------------------------------------------
# AC-1 — re_verify releases a failed gate whose tree is now green, on merit
# ----------------------------------------------------------------------


def test_re_verify_releases_failed_gate_when_tree_now_green(tmp_path):
    verifier = FakeVerifier()
    verifier.script = [dict(_FAIL), dict(_PASS)]
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="re-verify clears transient failure")
    _walk_to_gate(cond, t.id, _green_gate_id())

    # First, a fresh verifier FAIL latches gate_state='failed' (the transient).
    first = cond.gate_decide(t.id, action="approve", reason=_GREEN_REASON)
    assert first["ok"] is False
    assert task_svc.get(t.id).gate_state == "failed"

    # Now the tree is green — re_verify (NOT override) re-runs the verifier and
    # releases on merit.
    result = cond.gate_decide(
        t.id, action="approve", reason=_GREEN_REASON, re_verify=True)

    assert result["ok"] is True
    assert result["gate_state"] == "passed"
    assert result["verifier"]["status"] == "pass"
    # Honest release: NOT an override.
    assert result.get("override") is not True
    # The verifier was re-consulted fresh (once for the fail, once for re_verify).
    assert len(verifier.calls) == 2
    # NO manual-override audit row was written by the re_verify release.
    assert _manual_override_rows(task_svc, t.id) == []
    # A gate_decide row records the verifier pass under actor='conductor'.
    passed_rows = [r for r in task_svc.history(t.id)
                   if r.action == "gate_decide" and "verifier=pass" in r.details]
    assert passed_rows and passed_rows[-1].actor == "conductor"


# ----------------------------------------------------------------------
# AC-2 — a still-red slice cannot re-arm green via re_verify
# ----------------------------------------------------------------------


def test_re_verify_stays_failed_when_verifier_still_fails(tmp_path):
    verifier = FakeVerifier()
    verifier.script = [dict(_FAIL), dict(_FAIL)]
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="re-verify on a genuinely red slice")
    _walk_to_gate(cond, t.id, _green_gate_id())

    first = cond.gate_decide(t.id, action="approve", reason=_GREEN_REASON)
    assert first["ok"] is False

    result = cond.gate_decide(
        t.id, action="approve", reason=_GREEN_REASON, re_verify=True)

    assert result["ok"] is False
    assert result["gate_state"] == "failed"
    refreshed = task_svc.get(t.id)
    assert refreshed.gate_state == "failed"
    assert refreshed.gate_reason  # a fresh reason is recorded
    # No dishonest release: no manual-override row either.
    assert _manual_override_rows(task_svc, t.id) == []


# ----------------------------------------------------------------------
# AC-3 — re_verify needs a verifier to re-run
# ----------------------------------------------------------------------


def test_re_verify_requires_attached_verifier(tmp_path):
    task_svc, cond = _services(tmp_path, verifier=None)
    t = task_svc.create(title="re-verify with no verifier")
    _walk_to_gate(cond, t.id, _green_gate_id())
    # Latch failed via reject (no verifier available to fail it).
    cond.gate_decide(t.id, action="reject", reason="latched failed for test")
    assert task_svc.get(t.id).gate_state == "failed"

    result = cond.gate_decide(
        t.id, action="approve", reason=_GREEN_REASON, re_verify=True)

    assert result["ok"] is False
    assert "verifier" in result["reason"].lower()
    assert task_svc.get(t.id).gate_state == "failed"


# ----------------------------------------------------------------------
# AC-4 — override path unchanged; plain approve on a failed gate still refused
# ----------------------------------------------------------------------


def test_override_path_unchanged_on_failed_gate(tmp_path):
    verifier = FakeVerifier()
    verifier.script = [dict(_FAIL)]
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="override still recovers a failed gate")
    _walk_to_gate(cond, t.id, _green_gate_id())

    first = cond.gate_decide(t.id, action="approve", reason=_GREEN_REASON)
    assert first["ok"] is False
    calls_before = len(verifier.calls)

    # Distinct-actor override recovers the failed gate, tags manual-override,
    # and does NOT consult the verifier.
    result = cond.gate_decide(
        t.id, action="approve",
        reason=("qa judgment: env blip; independent re-run: "
                "pytest -q -> 320 passed, 0 failed"),
        override=True, actor="independent-verifier", session_id="iv-1")
    assert result["ok"] is True
    assert result["gate_state"] == "passed"
    assert result["override"] is True
    assert len(verifier.calls) == calls_before  # verifier NOT re-consulted
    assert _manual_override_rows(task_svc, t.id)  # audit row present


def test_failed_gate_plain_approve_still_refused(tmp_path):
    verifier = FakeVerifier()
    verifier.script = [dict(_FAIL)]
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="plain approve cannot clear a failed gate")
    _walk_to_gate(cond, t.id, _green_gate_id())

    first = cond.gate_decide(t.id, action="approve", reason=_GREEN_REASON)
    assert first["ok"] is False

    # No override, no re_verify -> still refused, gate stays failed.
    result = cond.gate_decide(t.id, action="approve", reason=_GREEN_REASON)
    assert result["ok"] is False
    assert task_svc.get(t.id).gate_state == "failed"
