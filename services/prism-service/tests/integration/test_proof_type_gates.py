"""RED scaffold — proof-type-driven gates (task 0e071d68).

Pins proof_type-driven gate verification through the REAL
ConductorService.gate_decide seam (the path MCP conductor_gate calls), not
isolated helpers — a test that merely called a new shape-validator would
pass even if the rule never reached the gate (the false-green this scaffold
exists to prevent).

  AC-1 — a NON-ui task with proof_type='metric' + a build-count
         completion_proof passes red_gate AND green_gate WITHOUT override.
  AC-2 — a `ui`-tagged task with proof_type='metric' is judged on the
         metric SHAPE (not the demo/screenshot requirement) at green_gate —
         passes BOTH ui_artifact_gate_reason and green_gate_artifact_reason.
  AC-3 — a proof_type='test' task still requires a runner+pass/fail trace
         (regression guard — the test path is not weakened).
  FR-3 — the tier0 consult is proof_type-aware: a metric oracle is NOT
         forced through the test-shaped 'green_full' expectation, so a metric
         task green-gates without override even when a blind verifier errors.

ALL the metric-path assertions FAIL today: ui_artifact_gate_reason
HARD-requires proof_type=='demo' (conductor_service.py:268), the red/green
artifact teeth keyword-match a TEST runner+pass/fail signal (a count-delta
carries neither, :290-339), and the tier0 rule maps green_gate->'green_full'
(status=pass) so a zero-claim non-test oracle errors the gate (:1347).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# A build-count / metric receipt: a numeric count-delta predicate. Carries
# NO test-runner / pass / fail / UI-artifact signal — so the ONLY thing that
# can clear it is a proof_type='metric'-aware shape validator.
_METRIC_PROOF = "build-count: default MCP tool surface trimmed 41 -> 31"
_METRIC_REASON = "metric receipt: default surface count 41 -> 31 (build-count)"
# A test-shaped reason used ONLY to walk intermediate gates (red_gate's
# artifact tooth wants a runner+fail signal); never at the gate under test.
_WALK_REASON = "walk gate; independent re-run: pytest -> 1 failed"


class _Verifier:
    """Pins the verifier verdict so the gate's outcome is driven by the
    proof_type teeth, not the Tier-0/Tier-1 machinery."""

    def __init__(self, result=None):
        self.calls = []
        self.next_result = result or {"status": "pass", "tier0": "pass",
                                      "tier1": "pass", "summary": "all green"}

    def run(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.next_result


def _services(tmp_path, verifier=None):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=verifier)
    return task_svc, cond


def _walk_to(cond, task_svc, task_id, target_gate, override=False):
    """Advance toward target_gate; approve any INTERMEDIATE pending gate so
    the test isolates target_gate. With override=False a test-shaped reason
    satisfies red_gate's artifact tooth (verifier=None path); with
    override=True a DISTINCT actor per clear avoids the self-override guard
    (verifier-driven path). Stops with target_gate pending."""
    from prism_service.models.workflow import WORKFLOW_STEPS
    target_idx = next(i for i, s in enumerate(WORKFLOW_STEPS)
                      if s["id"] == target_gate)
    guard, n = (target_idx + 1) * 3, 0
    while guard > 0:
        guard -= 1
        snap = task_svc.get(task_id)
        if snap.workflow_step == target_gate and snap.gate_state == "pending":
            return
        if snap.gate_state == "pending":
            n += 1
            kw = ({"override": True, "actor": f"walk-bot-{n}",
                   "session_id": f"walk-bot-{n}"} if override else {})
            cond.gate_decide(task_id, action="approve", reason=_WALK_REASON,
                             **kw)
            continue
        cond.advance_task(task_id)
    raise AssertionError(f"never reached {target_gate}")


# ── AC-1: non-ui metric task clears red_gate AND green_gate, no override ──

def test_metric_task_passes_red_and_green_without_override(tmp_path):
    task_svc, cond = _services(tmp_path, verifier=None)
    t = task_svc.create(title="trim default MCP surface", tags=["backend"],
                        proof_type="metric", completion_proof=_METRIC_PROOF)

    _walk_to(cond, task_svc, t.id, "red_gate")
    red = cond.gate_decide(t.id, action="approve", reason=_METRIC_REASON)
    assert red["ok"] is True, (
        "a proof_type='metric' task with a build-count proof was REJECTED at "
        f"red_gate WITHOUT override (reason={red.get('reason')!r}) — the "
        "artifact tooth must dispatch on proof_type, not demand a test trace")
    assert red["gate_state"] == "passed"
    assert red.get("override") is not True

    _walk_to(cond, task_svc, t.id, "green_gate")
    green = cond.gate_decide(t.id, action="approve", reason=_METRIC_REASON)
    assert green["ok"] is True, (
        "a proof_type='metric' task with a build-count proof was REJECTED at "
        f"green_gate WITHOUT override (reason={green.get('reason')!r})")
    assert green["gate_state"] == "passed"
    assert green.get("override") is not True


# ── AC-2: ui + proof_type=metric judged on the metric shape ──────────────

def test_ui_metric_task_passes_green_on_metric_shape(tmp_path):
    task_svc, cond = _services(tmp_path, verifier=None)
    t = task_svc.create(title="surface a metric on a card", tags=["ui"],
                        proof_type="metric", completion_proof=_METRIC_PROOF)
    _walk_to(cond, task_svc, t.id, "green_gate")
    green = cond.gate_decide(t.id, action="approve", reason=_METRIC_REASON)
    assert green["ok"] is True, (
        "a `ui` task with proof_type='metric' was REJECTED at green_gate — it "
        "must be judged on the metric SHAPE, not the demo/screenshot "
        f"requirement (reason={green.get('reason')!r})")
    assert green["gate_state"] == "passed"


def test_ui_metric_passes_both_artifact_helpers():
    """AC-2 names BOTH pure teeth. ui_artifact_gate_reason must DEFER to the
    declared proof_type (FR-4) and green_gate_artifact_reason must dispatch on
    proof_type to accept the metric shape (FR-2)."""
    from prism_service.services.conductor_service import (
        ui_artifact_gate_reason, green_gate_artifact_reason)
    assert ui_artifact_gate_reason(["ui"], "metric", _METRIC_PROOF) == "", (
        "ui_artifact_gate_reason still HARD-demands proof_type=='demo' for a "
        "ui task — it must defer to a declared non-demo proof_type")
    assert green_gate_artifact_reason(
        _METRIC_PROOF, _METRIC_REASON, proof_type="metric") == "", (
        "green_gate_artifact_reason rejects a metric build-count receipt — it "
        "must thread proof_type and accept the metric count-delta shape")


# ── AC-3: the test path is NOT weakened (regression guard) ───────────────

def test_test_proof_type_metric_only_proof_still_rejected(tmp_path):
    """REGRESSION: proof_type='test' is the DEFAULT shape. A metric-only
    receipt (no runner+pass) must STILL be rejected at green_gate — the
    proof_type fork must not let a non-test receipt clear a test task."""
    task_svc, cond = _services(tmp_path, verifier=None)
    t = task_svc.create(title="backend test-shaped", tags=["backend"],
                        proof_type="test", completion_proof=_METRIC_PROOF)
    _walk_to(cond, task_svc, t.id, "green_gate")
    green = cond.gate_decide(t.id, action="approve",
                             reason="claims green but no runner trace")
    assert green["ok"] is False, (
        "a proof_type='test' task with a metric-only receipt PASSED "
        "green_gate — the test-shaped artifact tooth was weakened")
    # Owner 2026-07-19: a refused approve stays PENDING (retryable), not failed.
    assert green["gate_state"] == "pending"


# ── FR-3: tier0 consult is proof_type-aware (metric not forced test-shaped) ─

def test_metric_gate_passes_despite_blind_test_shaped_verifier(tmp_path):
    """A non-test oracle yields zero claims -> tier0 'error'. The tier0
    consult must be proof_type-aware so a metric task is NOT forced through
    the test-shaped 'green_full' expectation; it green-gates WITHOUT override
    even when the verifier errors."""
    blind = _Verifier({"status": "error", "tier0": "error",
                       "summary": "no claims (non-test oracle)"})
    task_svc, cond = _services(tmp_path, blind)
    t = task_svc.create(title="metric under blind verifier", tags=["backend"],
                        proof_type="metric", completion_proof=_METRIC_PROOF)
    _walk_to(cond, task_svc, t.id, "green_gate", override=True)
    green = cond.gate_decide(t.id, action="approve", reason=_METRIC_REASON)
    assert green["ok"] is True, (
        "a proof_type='metric' task was FORCED to fail green_gate by a blind "
        "test-shaped verifier — the tier0 consult must be proof_type-aware "
        f"(reason={green.get('reason')!r})")
    assert green["gate_state"] == "passed"
