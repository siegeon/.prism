"""Distinct-actor role enforcement at the conductor gates.

The gate's Steward role must be INDEPENDENT of the actors that produced
the work: an 'approve' whose deciding actor is among the work-producing
sessions is refused (no self-override, task 3826dac3), while a DISTINCT
actor is allowed to clear the gate. Mirrors the no-self-override tests in
tests/unit/test_reinforce_sdlc_gates_real_proof.py, framed around the
role/tier engine's "gate is a separate role" doctrine.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class _PassVerifier:
    """A verifier that always passes — the override path never consults it,
    but ProjectContext always wires one, so mirror production."""

    def run(self, **kwargs: object) -> dict:
        return {"status": "pass", "tier0": "pass", "tier1": "pass",
                "tier2": "skipped", "summary": "green"}


def _services(tmp_path, verifier=None):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "scores.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False,
        task_svc=task_svc, verifier_svc=verifier,
    )
    return task_svc, cond


def _workflow():
    from prism_service.models.workflow import WORKFLOW_STEPS
    return WORKFLOW_STEPS


def _walk_to_gate(cond, task_id: str, gate_id: str) -> None:
    """Advance to `gate_id` (pending), clearing intermediate gates with a
    DISTINCT throwaway actor each (a reused actor would itself trip the
    same-actor guard on the next gate)."""
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this gate role-enforcement walk (unrelated to premise
    # content) can leave review_previous_notes.
    cond._task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising distinct-actor gate "
        "enforcement, not a real premise claim - UNVERIFIED\n"))
    target_idx = next(i for i, s in enumerate(_workflow()) if s["id"] == gate_id)
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
                reason=("walk intermediate; independent re-run: "
                        "pytest -q -> 2 failed / 0 passed"),
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}",
            )
            continue
        cond.advance_task(task_id)


# ----------------------------------------------------------------------
# The work-producing actor (S1) cannot clear its own gate; a distinct
# actor (S2) can. The gate's Steward role is INDEPENDENT of the work.
# ----------------------------------------------------------------------


def test_work_producing_actor_cannot_clear_its_own_gate(tmp_path):
    task_svc, cond = _services(tmp_path, verifier=_PassVerifier())
    t = task_svc.create(title="steward must be independent")
    _walk_to_gate(cond, t.id, "red_gate")

    # S1 produced the work on this task (prior linked session).
    assert task_svc.link_session(t.id, "S1") is True
    actors = {s["session_id"] for s in task_svc.sessions_for_task(t.id)}
    assert "S1" in actors, "S1 must be a recorded work-producing actor"

    refused = cond.gate_decide(
        t.id, action="approve", reason="i wrote it, trust me",
        override=True, actor="S1", session_id="S1",
    )
    assert refused["ok"] is False, "S1 (a work producer) must not self-clear"
    reason = (refused.get("reason") or "").lower()
    assert "actor" in reason, reason
    assert ("independent" in reason or "distinct" in reason
            or "same-actor" in reason), reason


def test_distinct_actor_is_allowed_to_clear_the_gate(tmp_path):
    task_svc, cond = _services(tmp_path, verifier=_PassVerifier())
    t = task_svc.create(title="independent steward clears")
    _walk_to_gate(cond, t.id, "red_gate")

    # S1 is the work producer; the gate is still pending after the refusal.
    task_svc.link_session(t.id, "S1")
    cond.gate_decide(
        t.id, action="approve", reason="self attempt",
        override=True, actor="S1", session_id="S1",
    )
    still = task_svc.get(t.id)
    assert still.workflow_step == "red_gate"
    assert still.gate_state == "pending", "refused self-override must not fail the gate"

    # S2 — a DISTINCT actor (the independent Steward) — clears it.
    allowed = cond.gate_decide(
        t.id, action="approve",
        reason="independent verifier re-ran: pytest -q -> 2 failed / 0 passed",
        override=True, actor="S2", session_id="S2",
    )
    assert allowed["ok"] is True, allowed
    assert allowed["gate_state"] == "passed", allowed
