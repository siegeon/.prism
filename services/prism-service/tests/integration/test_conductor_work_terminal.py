"""conductor_work terminal/`done` contract (task 7b219546).

The single loop verb computes `done` as *(task is on the FINAL step AND that
gate passed)* — NOT from a null next-job. This test pins WHY: advancing past
the terminal green_gate is a no-op (the state machine has no step after it),
so `_job()` keeps returning the green_gate job even once it has passed. A loop
that waited for a null job would spin forever — the exact `likely_misfire`
this task pre-declared. Uses the real ConductorService gate teeth + the real
distinct-actor rule (no mocks of the gate logic).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class _PassVerifier:
    def run(self, **kwargs: object) -> dict:
        return {"status": "pass", "tier0": "pass", "tier1": "pass",
                "tier2": "skipped", "summary": "green"}


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "scores.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False,
        task_svc=task_svc, verifier_svc=_PassVerifier(),
    )
    return task_svc, cond


def _terminal(task) -> bool:
    """The EXACT condition conductor_work's handler computes for `done`."""
    from prism_service.models.workflow import WORKFLOW_STEPS
    if str(getattr(task, "status", "") or "") in ("done", "cancelled"):
        return True
    last = WORKFLOW_STEPS[-1]["id"]
    return (getattr(task, "workflow_step", "") == last
            and getattr(task, "gate_state", "") == "passed")


def _drive_to_done(cond, task_svc, task_id: str) -> None:
    """Walk the whole SDLC the way the conductor_work loop does: advance agent
    steps, and clear each gate with a DISTINCT verifier actor carrying a real
    test-shaped proof (satisfies the proof-carrying artifact tooth)."""
    from prism_service.models.workflow import WORKFLOW_STEPS

    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this terminal/`done`-contract walk (unrelated to premise
    # content) can leave review_previous_notes.
    task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising the terminal/`done` "
        "contract, not a real premise claim - UNVERIFIED\n"))
    proof = "independent re-run: pytest -q -> 3 passed / 0 failed (all green)"
    guard = len(WORKFLOW_STEPS) * 3
    seat = 0
    while guard > 0:
        guard -= 1
        t = task_svc.get(task_id)
        if _terminal(t):
            return
        step = next((s for s in WORKFLOW_STEPS
                     if s["id"] == (t.workflow_step or "")), None)
        if step is None:
            cond.advance_task(task_id, session_id="builder")
            continue
        if step["type"] == "gate" and t.gate_state == "pending":
            seat += 1
            # completion_proof carries the artifact; a DISTINCT actor clears it.
            task_svc.update(task_id, completion_proof=proof)
            cond.gate_decide(task_id, action="approve", reason=proof,
                             override=True, actor=f"verifier-{seat}",
                             session_id=f"verifier-{seat}")
        else:
            cond.advance_task(task_id, session_id="builder")


def test_done_is_final_step_plus_gate_passed(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="drive to done via the loop")
    _drive_to_done(cond, task_svc, t.id)

    final = task_svc.get(t.id)
    assert _terminal(final) is True, (
        f"loop must reach done: step={final.workflow_step} "
        f"gate={final.gate_state}")
    from prism_service.models.workflow import WORKFLOW_STEPS
    assert final.workflow_step == WORKFLOW_STEPS[-1]["id"] == "green_gate"
    assert final.gate_state == "passed"


def test_advance_past_green_gate_is_a_noop_so_next_job_never_nulls(tmp_path):
    """Guards the likely_misfire: if `done` keyed off a null next-job, the loop
    would never terminate — because advancing past green_gate does NOT null the
    step."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="terminal no-op")
    _drive_to_done(cond, task_svc, t.id)

    # green_gate passed. Advancing again is refused (already final) and the
    # step is UNCHANGED — so _step_by_id still resolves a (non-null) job.
    res = cond.advance_task(t.id, session_id="builder")
    assert res["ok"] is False
    assert "final" in (res.get("reason") or "").lower()
    from prism_service.services.conductor_service import ConductorService
    still = task_svc.get(t.id)
    assert ConductorService._step_by_id(still.workflow_step) is not None, (
        "next_job is NOT null at terminal — done must be computed, not inferred")
