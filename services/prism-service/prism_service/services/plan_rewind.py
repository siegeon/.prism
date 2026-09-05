"""A refused plan or story rubric rewinds the drive (task fb997b1d).

THE DEADLOCK THIS CLOSES. A task whose plan or story rubric REFUSES could
not be moved by anything:
  - `task_runner.eligible_task` skips it, because its step is a gate
    (`task_runner.py:544`).
  - `gate_adjudicator` withholds it rather than approving, because a
    deterministic tooth refused (`gate_adjudicator.py:221-228`).
  - `dispatch.after_step` will not drive a gate step, because a gate
    belongs to a distinct seat.
So the only way out was a person hand-editing `plan_doc`. Measured live on
2026-08-31: tasks a928f3d5 and 02264017 were both wedged this way.

TWO REWINDS ALREADY EXISTED AND NEITHER COVERED THIS.
`green_rewind.maybe_rewind` fires only on a FAILED EvidenceReceipt at
green_gate. `ConductorService._auto_rewind` (7.13.133) fires only from
`gate_decide`, i.e. only when an actor explicitly REJECTS. The gap was the
case where NOBODY rejects: the rubric refuses, the adjudicator withholds,
and the row simply stops.

THE REFUSAL SOURCE DIFFERS PER GATE, and conflating them ships half of this
module as dead code. There is no `story_gate_checks.py` in this repo:
  - `plan_gate` reads `plan_gate_checks.refusal()`, whose three teeth read
    plan-phase content (plan_doc, verify, stop_if against a base ref).
  - `story_gate` reads the rubric verdict, the same data
    `gate_adjudicator._pending_decline_reason` already surfaces for it.
Asking the plan teeth about a story returns "" almost always, and an empty
refusal means "nothing to rewind for", so that half would never fire.

WHAT IT DELIBERATELY DOES NOT TOUCH. A plan_gate held for the ROOT-task
owner approval (`design_packet.root_plan_gate_escalation_reason`) is a
human stop, not a rubric refusal, and it is excluded for free because this
module reads only the rubric sources above. A plan_gate refused by the
arc_governance/plan_coverage rubric is likewise out of scope and stays
parked.

CODIFIED: deterministic Python reading a verdict and moving a row. It costs
no tokens, which is the point -- it replaces a human hand-edit.
"""

from __future__ import annotations

import json
import os
from typing import Optional

DEFAULT_REWIND_BUDGET = 3
REWIND_ACTOR = "conductor-adjudicator"
REWIND_ACTION = "rewind"

# Each rubric gate rewinds to the AGENT step immediately before it, taken
# from WORKFLOW_STEPS order. Being an agent step is exactly the property
# `task_runner.eligible_task` tests, so this is what makes the task
# reachable by a drive seat again.
_STEP_BEFORE = {
    "plan_gate": "verify_plan",
    "story_gate": "draft_story",
}


def _source_path(project: str) -> str:
    """Repo root whose conductor behavior entry holds the budget."""
    return os.environ.get("PRISM_SOURCE_PATH") or os.getcwd()


def rewind_budget(project: str) -> int:
    path = os.path.join(_source_path(project), ".prism", "behaviors",
                        "conductor.json")
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh).get("rewind_budget")
        return int(value) if value is not None else DEFAULT_REWIND_BUDGET
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_REWIND_BUDGET


def rewind_count(task_svc, task_id: str, from_step: str) -> int:
    """Rewinds already spent ON THIS GATE.

    Scoped to the from-step on purpose. `green_rewind.rewind_count` counts
    EVERY action="rewind" row with no gate filter, so reusing it verbatim
    would let a green rewind spend the plan budget.
    """
    marker = f"{from_step} ->"
    return sum(1 for h in task_svc.history(task_id)
               if h.action == REWIND_ACTION and marker in (h.details or ""))


def _refusal_for(ctx, task, step: str, project: str) -> str:
    """The rubric's own words for why `step` did not clear, or "".

    An empty string means the rubric did not refuse, or could not judge.
    Either way there is nothing to rewind for -- "cannot judge" is NOT
    "refused", the distinction `green_rewind` shipped wrong once (7.13.190)
    by testing a boolean that was False for every non-pass.
    """
    try:
        if step == "story_gate":
            conductor = getattr(ctx, "conductor_svc", None)
            if conductor is None:
                return ""
            validation = conductor._validation_for_gate(step)
            if not validation:
                return ""
            check = conductor._verify_rubric_gate(task, validation) or {}
            if check.get("verified") is not True:
                return str(check.get("reason") or "")
            return ""
        if step == "plan_gate":
            from prism_service.services import plan_gate_checks
            return str(plan_gate_checks.refusal(task, project) or "")
    except Exception:  # noqa: BLE001 - a rewind never breaks the sweep
        return ""
    return ""


def maybe_rewind(ctx, task, project: str) -> Optional[dict]:
    """Rewind a task parked at a REFUSED rubric gate.

    Returns None when nothing applies (another step, a decided gate, no
    refusal), {"ok": True, ...} on a rewind, {"ok": False, "parked": True}
    when the budget is spent, and {"ok": False, "inconclusive": True} when
    the rubric gave no actionable refusal.
    """
    step = str(getattr(task, "workflow_step", "") or "")
    destination = _STEP_BEFORE.get(step)
    if destination is None:
        return None
    # ONLY A PENDING GATE. Undoing a decision an actor already made would
    # erase a real judgement, a human's included.
    if str(getattr(task, "gate_state", "") or "") != "pending":
        return None

    task_svc = ctx.task_svc
    refusal = _refusal_for(ctx, task, step, project).strip()
    if not refusal:
        # RECORD THE REASON AT THE SEAT. A tooth that computes a verdict and
        # returns it without writing it leaves the gate parked with an EMPTY
        # gate_reason, and no driver can self-diagnose why it did not move.
        reason = (f"{step}: the rubric gave no actionable refusal, "
                  f"so there is nothing to rewind for.")
        task_svc.update(task.id, gate_reason=reason)
        return {"ok": False, "inconclusive": True, "task_id": task.id,
                "step": step, "reason": reason}

    budget = rewind_budget(project)
    spent = rewind_count(task_svc, task.id, step)
    if spent >= budget:
        reason = (f"Rewind budget {budget} spent at {step}, and the rubric "
                  f"still refuses, {refusal}")
        task_svc.update(task.id, gate_reason=reason)
        return {"ok": False, "parked": True, "task_id": task.id,
                "step": step, "budget": budget, "reason": reason}

    attempt = spent + 1
    # QUOTE THE CLAUSE VERBATIM. gate_reason is STE-normalised on write
    # (task_service.py:751, flavored mode), and a semicolon there becomes a
    # sentence break that capitalises the next word -- so "plan_checks: ..."
    # would reach the next drive as "Plan_checks: ...", no longer the string
    # the rubric emitted and no longer greppable against it.
    reason = (f"Rewind {attempt}/{budget}: {step} rubric refused, {refusal}")
    # A REWIND LANDS ON AN AGENT STEP, WHICH HAS NO GATE. Writing "pending"
    # here would leave a row `task_service.is_open_gate_step` cannot see --
    # the blind spot that let a stall handler close task 8fbd5cf0 as done
    # while its gate had never been decided.
    task_svc.update(task.id, workflow_step=destination, gate_state="none",
                    gate_reason=reason, status="in_progress")
    task_svc.record_history(
        task.id, action=REWIND_ACTION,
        details=(f"{step} -> {destination}; attempt={attempt}/{budget}; "
                 f"refusal={refusal[:200]}"),
        actor=REWIND_ACTOR)
    return {"ok": True, "task_id": task.id, "from_step": step,
            "to_step": destination, "attempt": attempt, "budget": budget,
            "refusal": refusal}
