"""Inverted pull-loop over the REAL conductor (task e825e00a).

Additive read-through + advance wrapper: turns the existing conductor
state machine into a job dispenser without touching conductor_service.
A worker (any MCP client OR curl) drives a real task with:

  start(task_id)  -> enter the flow, get the first job
  next(task_id)   -> what job is on deck right now (read-only)
  report(task_id) -> record outcome; the SERVER advances (advance_task
                     for agent steps, gate_decide for gates) and hands
                     back the next job.

The worker never encodes the SDLC sequence — the conductor owns it. Gate
jobs enforce the distinct-actor rule by session identity here, up front,
so a producer can never clear its own gate.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from prism_service.project_context import get_project
from prism_service.services.conductor_service import ConductorService
from prism_service.services import task_workspace
from prism_service.services.context_builder import role_rule_assets

router = APIRouter()

_ROLE = {"sm": "Steward", "qa": "Verifier", "dev": "Builder"}

_GUIDE = {
    "review_previous_notes": "Review prior notes/decisions for this task.",
    "draft_story": "Author the story: Summary/Requirements/Acceptance "
                   "Criteria with AC ids + oracles.",
    "verify_plan": "Verify the plan covers the story (plan_coverage).",
    "write_failing_tests": "Author failing tests carrying a trace.",
    "implement_tasks": "Smallest change that turns the failing tests green.",
    "verify_green_state": "Verify full green against a real run.",
}


def _svc(project: str):
    return get_project(project).conductor_svc


def _job(task) -> Optional[dict]:
    """Build the self-describing job for a task's CURRENT step, or None
    when the task hasn't entered the flow yet."""
    step = ConductorService._step_by_id(task.workflow_step)
    if step is None:
        return None
    kind = step["type"]
    role = step.get("agent") or ""
    base = ("GATE: an independent, DISTINCT actor decides."
            if kind == "gate" else _GUIDE.get(step["id"], step["id"]))
    # ROLE-CONDITIONED DOCTRINE (fd297cf0): splice the SAME role-scoped rules
    # context_bundle composes into the per-job instructions, so the dumb queue
    # worker is TOLD what to follow without calling context_bundle itself.
    # Agent steps only, keyed by the step's role — gates stay doctrine-free so a
    # reviewer is never nudged to be lazy; qa/sm never receive the Builder's
    # minimum-change rule (role_rule_assets enforces the allow-list).
    doctrine = ([a.content for a in role_rule_assets(role)]
                if kind != "gate" else [])
    instructions = base
    if doctrine:
        instructions = base + "\n\nRules for this role (" + (role or "-") \
            + "):\n" + "\n".join(f"- {d}" for d in doctrine)
    return {
        "task_id": task.id,
        "step": step["id"],
        "kind": kind,
        "role": role,
        "role_label": _ROLE.get(role, "-"),
        "gate_state": task.gate_state,
        "instructions": instructions,
        "doctrine": doctrine,
        "expected_proof": step.get("validation") or (
            "prior step's validation" if kind == "gate" else "n/a"),
    }


class Ident(BaseModel):
    task_id: str
    session_id: Optional[str] = None
    outcome: object = ""
    model: Optional[str] = None
    # expected_step names the step this report is FOR. flow_report REQUIRES
    # it (see there) so a stale/duplicate report cannot advance whatever
    # step is now current; flow_start ignores it.
    expected_step: Optional[str] = None
    # Optional queue claim id (job_queue.py leases). Carried for audit; the
    # conductor flow is idempotent on expected_step, not the lease.
    job_id: Optional[str] = None
    # Gates enforce for REAL by default (rubric for story/plan, verifier +
    # artifact for red/green). override is an explicit, audited exception —
    # a genuine independent reviewer forcing a decision — never the default.
    override: bool = False


# A worker's outcome only signals FAILURE when it is UNAMBIGUOUS — an
# explicit token or a structured ok/success=false / status in this set. A
# free-text narrative (a gate-approval reason, "pytest -q -> 2 failed / 0
# passed") is NOT a failure, so we never misread a reason that merely
# mentions "failed". Success (anything else) is what advances.
_FAILURE_TOKENS = {"failure", "failed", "fail", "error", "errored",
                   "blocked", "false", "reject", "rejected"}


def _is_failure(outcome: object) -> bool:
    if outcome is None:
        return False
    if isinstance(outcome, bool):
        return outcome is False
    if isinstance(outcome, dict):
        if outcome.get("ok") is False or outcome.get("success") is False:
            return True
        for key in ("status", "outcome", "result", "state"):
            v = outcome.get(key)
            if isinstance(v, str) and v.strip().lower() in _FAILURE_TOKENS:
                return True
        return False
    if isinstance(outcome, str):
        return outcome.strip().lower() in _FAILURE_TOKENS
    return False


# Gates whose validation is a PURE rubric function (arc_governance scored on
# the task's own evidence) auto-clear on a machine PASS — the rubric IS the
# independent judge, so parking a green verdict on a human click is ceremony
# (the owner's 2026-07-13 'ease up the process' directive). red/green gates
# stay human: their checks run the shell verifier / oracle receipt in the
# daemon env, which is not yet trustworthy enough to sign alone
# (inverted-flow soundness gap: full-suite-in-daemon is env-fragile).
_AUTOCLEAR_GATES = {"story_gate", "plan_gate"}


def _autoclear_machine_gate(svc, task_id: str) -> Optional[dict]:
    """Approve a just-entered PENDING rubric gate iff its machine check says
    verified=True. Anything else (fail, None, non-rubric gate) is left
    pending for a human. Runs as the server's own seat — distinct from every
    producing session by construction."""
    task = svc._task_svc.get(task_id)
    if task is None or getattr(task, "gate_state", "") != "pending":
        return None
    step = ConductorService._step_by_id(task.workflow_step)
    if step is None or step["type"] != "gate":
        return None
    if step["id"] == "green_gate":
        # MACHINE ADJUDICATOR SEAT (task 1d3322a6): the flow just minted at
        # verify_green_state, so a fresh passing EvidenceReceipt can approve
        # the gate in seconds. SHIPS OFF BY DEFAULT (owner 2026-07-15:
        # human clicks stay the norm) — only environments that opted in via
        # PRISM_GATE_ADJUDICATOR_INTERVAL adjudicate here.
        from prism_service.services import gate_adjudicator
        if gate_adjudicator.is_enabled():
            return svc.adjudicate_green_gate(task_id)
        return None
    if step["id"] not in _AUTOCLEAR_GATES:
        return None
    check = svc._verify_gate(task, step["id"],
                             getattr(task, "proof_type", None))
    if check.get("verified") is not True:
        return None
    return svc.gate_decide(
        task_id, "approve",
        reason=("auto-clear: machine check passed — "
                + str(check.get("reason", ""))),
        session_id="conductor-autoclear", model="machine")


@router.get("/next")
def flow_next(task_id: str, project: str = Query("default")) -> dict:
    svc = _svc(project)
    task = svc._task_svc.get(task_id)
    if task is None:
        return {"job": None, "error": "unknown task"}
    return {"job": _job(task)}


@router.post("/start")
def flow_start(body: Ident, project: str = Query("default")) -> dict:
    svc = _svc(project)
    task = svc._task_svc.get(body.task_id)
    if task is None:
        return {"ok": False, "error": "unknown task"}
    # HONEST LOOP: a real git worktree of the PRISM repo is REQUIRED before
    # the flow may start — the red/green gates verify against THIS checkout.
    # FAIL CLOSED: if a real worktree cannot be created we REFUSE to start
    # rather than silently sharing the current branch (cross-task contam).
    try:
        ws = task_workspace.ensure_workspace(body.task_id)
    except Exception as exc:
        return {"ok": False,
                "error": f"workspace unavailable, refusing to start "
                         f"(fail closed): {exc}",
                "workspace": None}
    if not task.workflow_step:
        svc.advance_task(body.task_id, session_id=body.session_id,
                         model=body.model)
        task = svc._task_svc.get(body.task_id)
    return {"ok": True, "job": _job(task), "workspace": ws}


@router.get("/workspace")
def flow_workspace(task_id: str) -> dict:
    """Where the worker does its real work for this task. The verifier reads
    the same path, so the worker's committed tests are what gates check."""
    ws = task_workspace.workspace_for(task_id)
    return {"workspace": ws}


@router.post("/report")
def flow_report(body: Ident, project: str = Query("default")) -> dict:
    """Record the worker's outcome and let the SERVER advance the flow —
    but SOUNDLY: a report only advances on SUCCESS, must name the step it is
    for (idempotent + stale-safe), and must carry a session identity so the
    distinct-actor gate rule is trustworthy. 'worker reported' is NOT
    'step completed'."""
    svc = _svc(project)
    # (3) REQUIRE SESSION IDENTITY — distinct-actor gate enforcement is only
    # trustworthy if every report names its actor. No session -> reject.
    if not (body.session_id and str(body.session_id).strip()):
        return {"ok": False, "error": "session_id is required on a report "
                "(distinct-actor gate enforcement needs a named actor)"}
    task = svc._task_svc.get(body.task_id)
    if task is None:
        return {"ok": False, "error": "unknown task"}
    step = ConductorService._step_by_id(task.workflow_step)
    if step is None:
        return {"ok": False, "error": "task has not started the flow"}

    # (2) IDEMPOTENT + STALE-SAFE — the report must name the step it is FOR.
    # A report whose expected_step != the task's current step is stale or a
    # duplicate (the step already advanced, or the worker is out of sync):
    # no-op, so a late report can never advance whatever step is now current.
    # This also makes the desync case (task already sitting on a pending
    # gate) a benign no-op that echoes the true current step, not a failure.
    if not (body.expected_step and str(body.expected_step).strip()):
        return {"ok": False, "step": step["id"],
                "error": "expected_step is required — name the current step "
                "this report is for so a stale report cannot advance"}
    if body.expected_step != step["id"]:
        return {"ok": False, "noop": True, "step": step["id"],
                "expected_step": body.expected_step,
                "reason": "stale/duplicate report: expected_step does not "
                "match the task's current step; not advancing",
                "next_job": _job(task)}

    failed = _is_failure(body.outcome)

    if step["type"] == "gate":
        # Distinct-actor, enforced up front by session identity: a worker
        # who produced any prior step of this task may not clear its gate.
        producers = []
        try:
            producers = [s.get("session_id")
                         for s in svc._task_svc.sessions_for_task(body.task_id)]
        except Exception:
            producers = []
        if body.session_id in producers:
            return {"ok": False, "step": step["id"],
                    "reason": "distinct-actor: the producing session cannot "
                              "clear its own gate — route to a distinct worker "
                              "(or the claude -p adjudicator)",
                    "producers": producers}
        if failed:
            # (1) A reported FAILURE at a gate is a REJECT, never an approve —
            # it records the failure (gate_state=failed) and does NOT advance.
            res = svc.gate_decide(body.task_id, "reject",
                                  reason=str(body.outcome) or "flow rejection",
                                  session_id=body.session_id, model=body.model)
        else:
            # Approve on MERIT: no blanket override. story/plan are scored by
            # their YAML rubric (plan_doc/plan_diagram); red/green run the
            # verifier + the proof-carrying artifact tooth. A fabricated or
            # missing proof is refused here, exactly as it should be.
            res = svc.gate_decide(body.task_id, "approve",
                                  reason=str(body.outcome) or "flow approval",
                                  override=body.override, session_id=body.session_id,
                                  model=body.model)
    elif failed:
        # (1) OUTCOME-AWARE ADVANCE — a reported failure on an agent step
        # records a history row and LEAVES the task on the SAME step. The
        # worker reporting is not the step being done.
        try:
            svc._task_svc.record_history(
                body.task_id, action="flow_report_failure",
                details=f"step={step['id']}; outcome={str(body.outcome)[:200]}",
                actor=body.session_id)
        except Exception:
            pass
        return {"ok": False, "step": step["id"], "advanced": False,
                "reason": "reported failure: step not advanced (a reported "
                "outcome is not step completion)",
                "next_job": _job(task)}
    else:
        # MINT GREEN EVIDENCE at verify_green (inverted-flow #5): a SUCCESS
        # report on the verify_green_state step runs the 3-lane honest signal
        # (oracle receipt + red->green continuity + baseline-diff regression)
        # in a clean isolated env from the task's worktree, so the oracle
        # EvidenceReceipt the following green_gate requires is produced HERE —
        # before the advance — instead of expecting a self-attested proof. It
        # is best-effort: a lane error never blocks the advance (the gate's own
        # fresh-receipt tooth still refuses on a missing/failing receipt).
        if step["id"] == "verify_green_state":
            try:
                svc.mint_green_evidence(body.task_id,
                                        session_id=body.session_id,
                                        model=body.model)
            except Exception:
                pass
        res = svc.advance_task(body.task_id, session_id=body.session_id,
                              model=body.model)

    # If the advance parked the task on a rubric gate that already scores
    # green, clear it now — no human click for a machine-verified pass.
    _autoclear_machine_gate(svc, body.task_id)

    nxt = svc._task_svc.get(body.task_id)
    return {"ok": res.get("ok", False), "advanced": res,
            "next_job": _job(nxt)}
