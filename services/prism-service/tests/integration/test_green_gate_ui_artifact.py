"""RED scaffold — STRAND C: UI-artifact requirement at green_gate
(task 56458db1).

VERIFIER-BLINDNESS (implement.js:46-54): the conductor gate verifier scopes
Tier-0 git to the MCP daemon's cwd, not the working checkout, so it can
return a non-fail verdict on a real change and the agent OVERRIDEs the
blind gate with its own test trace. The hole: a `ui`-tagged task can
green-gate on a pytest line ALONE — no proof the UI actually renders. Per
the UI-FIRST mandate every feature ships a UI surface; a unit-test line is
not a demonstrable UI.

This task makes green_gate UI-aware THROUGH THE REAL DISPATCHER
(ConductorService.gate_decide — the seam MCP conductor_gate calls), not a
new isolated helper:
  C1 — a `ui` task's green_gate is REJECTED when completion_proof cites
       only a pytest/unit line and no UI artifact.
  C2 — a `ui` task's green_gate PASSES when completion_proof cites a real
       UI artifact path (agent-browser/verify screenshot vs :8888, or a
       Playwright assertion) AND proof_type=='demo'.
  C3 — non-`ui` tasks are UNAFFECTED (existing green_gate behavior is a
       regression guard).

These drive the REAL ConductorService gate_decide on the REAL green_gate
step with a FakeVerifier pinned to PASS — so the ONLY thing that can flip
the verdict is the net-new UI-artifact rule. A test that merely called a
new is_ui_demo() helper would pass even if the rule never reached the
gate (the false-green this scaffold exists to prevent).
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
    """Always returns a clean PASS so the gate's verdict is driven solely
    by the new UI-artifact rule, not the Tier-0/Tier-1 machinery."""

    def __init__(self, result: Optional[dict] = None) -> None:
        self.calls: list[dict] = []
        self.next_result = result or {
            "status": "pass", "tier0": "pass", "tier1": "pass",
            "tier2": "skipped", "summary": "all green",
        }

    def run(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        return self.next_result


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


def _green_gate_id() -> str:
    from prism_service.models.workflow import WORKFLOW_STEPS

    return next(s["id"] for s in WORKFLOW_STEPS
                if s["type"] == "gate" and s["id"] == "green_gate")


def _walk_to_green_gate(cond, task_id: str) -> None:
    """Advance to green_gate, clearing earlier pending gates via override
    so the test isolates the green_gate decision."""
    from prism_service.models.workflow import WORKFLOW_STEPS

    target_idx = next(i for i, s in enumerate(WORKFLOW_STEPS)
                      if s["id"] == _green_gate_id())
    guard = (target_idx + 1) * 3
    cleared = 0
    while guard > 0:
        guard -= 1
        snap = cond._task_svc.get(task_id)
        if snap.workflow_step == _green_gate_id() and snap.gate_state == "pending":
            return
        if snap.gate_state == "pending":
            # DISTINCT actor per clear (task 8579d49e): story_gate/plan_gate
            # precede green_gate; a reused actor self-overrides -> refused.
            cleared += 1
            cond.gate_decide(
                task_id, action="approve",
                reason="walk intermediate; independent re-run: pytest -> 1 failed",
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}")
            continue
        cond.advance_task(task_id)


# ── C1: ui task — pytest-only proof is REJECTED at green_gate ────────────

def test_ui_task_green_gate_rejected_with_pytest_only_proof(tmp_path):
    """A `ui`-tagged task with proof_type UNSET whose completion_proof cites
    ONLY a pytest line (no UI artifact) must FAIL green_gate even though the
    verifier passes. AC-7/FR-4: the demo/screenshot requirement applies when
    proof_type is unset or 'demo' — a declared non-demo proof_type opts out."""
    verifier = FakeVerifier()
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(
        title="ui feature — pytest-only proof",
        tags=["ui"],
        proof_type="",  # unset -> demo/screenshot requirement applies (FR-4)
        completion_proof="tests/unit/test_thing.py::test_renders PASSED",
    )
    _walk_to_green_gate(cond, t.id)

    result = cond.gate_decide(
        t.id, action="approve",
        reason="test: ui task with pytest-only proof",
    )

    assert result["ok"] is False, (
        "a `ui` task green-gated on a pytest line with NO UI artifact — the "
        "UI-FIRST mandate requires a demonstrable UI surface, not a unit test"
    )
    # Owner 2026-07-19: a refused approve must NOT strand the ticket into
    # 'failed' (which then demands recover-with-override). It stays PENDING with
    # the actionable reason so the operator attaches the artifact and re-approves.
    assert result["gate_state"] == "pending"
    refreshed = task_svc.get(t.id)
    assert refreshed.gate_state == "pending"
    assert refreshed.workflow_step == _green_gate_id()
    # The refusal reason must name the missing UI artifact so the operator
    # knows WHY (progressive-disclosure-friendly, actionable).
    assert "ui" in (result.get("reason") or "").lower() or "artifact" in (
        result.get("reason") or "").lower(), (
        "green_gate rejected the ui task but the reason does not mention the "
        "missing UI artifact"
    )


# ── C2: ui task — real UI artifact + proof_type=demo PASSES ──────────────

def test_ui_task_green_gate_passes_with_demo_artifact(tmp_path):
    """A `ui` task whose completion_proof cites a real UI artifact path
    (agent-browser/verify screenshot vs :8888) with proof_type='demo'
    must PASS green_gate."""
    verifier = FakeVerifier()
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(
        title="ui feature — demo artifact proof",
        tags=["ui"],
        proof_type="demo",
        completion_proof=(
            "agent-browser screenshot vs http://127.0.0.1:8888/conductor — "
            "docs/conductor_live_tokens.png"
        ),
    )
    _walk_to_green_gate(cond, t.id)

    result = cond.gate_decide(
        t.id, action="approve",
        reason="test: ui task with demo screenshot artifact",
    )

    assert result["ok"] is True, (
        "a `ui` task with a real UI artifact + proof_type=demo was rejected "
        f"at green_gate (reason={result.get('reason')!r})"
    )
    assert result["gate_state"] == "passed"


def test_ui_task_green_gate_metric_judged_on_metric_shape(tmp_path):
    """AC-7: under proof_type-driven enforcement a `ui` task that DECLARES
    proof_type='metric' is judged on the metric SHAPE, not the demo/screenshot
    requirement — a build-count receipt PASSES green_gate without override.
    (Supersedes the old 'ui must be proof_type=demo' coupling: proof_type now
    picks WHICH artifact shape is judged, per ADR mx-b0f363 / D3.)"""
    verifier = FakeVerifier()
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(
        title="ui feature — metric proof",
        tags=["ui"],
        proof_type="metric",
        completion_proof="build-count: default MCP tool surface 41 -> 31",
    )
    _walk_to_green_gate(cond, t.id)

    result = cond.gate_decide(
        t.id, action="approve",
        reason="metric receipt: default surface 41 -> 31 (build-count)",
    )

    assert result["ok"] is True, (
        "a `ui` task with proof_type='metric' was rejected at green_gate — "
        "proof_type must pick WHICH artifact shape is judged; a declared "
        f"metric is judged on its count-delta, not demo (reason={result.get('reason')!r})"
    )
    assert result["gate_state"] == "passed"


# ── C3: non-ui tasks UNAFFECTED (regression guard) ───────────────────────

def test_non_ui_task_green_gate_passes_with_test_proof(tmp_path):
    """REGRESSION GUARD: a NON-`ui` task with a pytest proof must still PASS
    green_gate exactly as before — the new UI-artifact requirement must not
    leak onto non-ui tasks."""
    verifier = FakeVerifier()
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(
        title="backend feature — pytest proof",
        tags=["backend"],
        proof_type="test",
        completion_proof="tests/unit/test_backend.py PASSED",
    )
    _walk_to_green_gate(cond, t.id)

    result = cond.gate_decide(
        t.id, action="approve",
        reason="test: non-ui task with pytest proof",
    )

    assert result["ok"] is True, (
        "REGRESSION: a non-`ui` task was rejected at green_gate — the new "
        "UI-artifact requirement leaked onto tasks without the `ui` tag"
    )
    assert result["gate_state"] == "passed"


def test_untagged_task_green_gate_passes_with_test_proof(tmp_path):
    """REGRESSION GUARD: an UNTAGGED task (no tags at all) keeps the legacy
    verifier-driven green_gate behavior."""
    verifier = FakeVerifier()
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(
        title="untagged feature",
        proof_type="test",
        completion_proof="tests/unit/test_x.py PASSED",
    )
    _walk_to_green_gate(cond, t.id)

    result = cond.gate_decide(
        t.id, action="approve", reason="test: untagged task green path",
    )

    assert result["ok"] is True, (
        "REGRESSION: an untagged task was rejected at green_gate by the new "
        "UI-artifact rule"
    )
    assert result["gate_state"] == "passed"
