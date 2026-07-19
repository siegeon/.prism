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

import subprocess
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
        # task 8f48f9bb — surface WHY a pending gate parked directly on the job
        # the driver loops on, so a driving agent self-diagnoses (e.g. "story_
        # complete: no acceptance criteria with AC-<n> ids found") without a
        # second fetch. Empty until the adjudicator sweep stamps it.
        "gate_reason": getattr(task, "gate_reason", "") or "",
        "instructions": instructions,
        "doctrine": doctrine,
        "expected_proof": step.get("validation") or (
            "prior step's validation" if kind == "gate" else "n/a"),
        # Worker contract (fb2846dc): the STRUCTURED allowed_files/verify/
        # stop_if the task was given — a first-class job field the worker
        # can read deterministically, not prose it is merely trusted to
        # parse out of `instructions`. Always present (empty lists on a
        # contract-less task) so callers never branch on key presence.
        "contract": {
            "allowed_files": list(getattr(task, "allowed_files", None) or []),
            "verify": list(getattr(task, "verify", None) or []),
            "stop_if": list(getattr(task, "stop_if", None) or []),
        },
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
    # Worker contract (fb2846dc): an audited escape hatch for the
    # implement_tasks out-of-allowlist refusal below — names every offending
    # path the worker knowingly touched outside allowed_files. Only consulted
    # when a refusal would otherwise fire; recorded to task history, never a
    # silent bypass.
    acknowledged_out_of_contract: Optional[list[str]] = None


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
        res = (svc.adjudicate_green_gate(task_id)
               if gate_adjudicator.is_enabled() else None)
        if res:
            return res
        # Parked for the HUMAN sign-off — write an actionable reason so the
        # field itself says WHAT to do (was blank; owner 2026-07-19). The
        # readiness card already carries the prompt; this makes the raw task
        # consistent for a driver reading gate_reason.
        try:
            _t = svc._task_svc.get(task_id)
            if _t and getattr(_t, "gate_state", "") == "pending" \
                    and not (getattr(_t, "gate_reason", "") or "").strip():
                svc._task_svc.update(task_id, gate_reason=(
                    "awaiting your sign-off — the evidence is ready; review it "
                    "and Approve (the single human decision for this ticket)"))
        except Exception:
            pass
        return None
    if step["id"] == "red_gate":
        # Demo-proof tickets only (task 59ddfcbc) — same opt-in switch.
        from prism_service.services import gate_adjudicator
        if gate_adjudicator.is_enabled():
            return svc.adjudicate_demo_red_gate(task_id)
        return None
    if step["id"] not in _AUTOCLEAR_GATES:
        return None
    check = svc._verify_gate(task, step["id"],
                             getattr(task, "proof_type", None))
    if check.get("verified") is not True:
        # Park WITH the actionable reason NOW (owner 2026-07-19): a rubric gate
        # must never sit pending with a BLANK gate_reason the driver can't act
        # on. Previously the reason only appeared on the next ~20s adjudicator
        # resweep, so the first poll saw 'pending' + '' and no signal.
        _r = str(check.get("reason", "") or "")
        if _r and _r != (getattr(task, "gate_reason", "") or ""):
            try:
                svc._task_svc.update(task_id, gate_reason=_r)
            except Exception:
                pass
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


# ---------------------------------------------------------------------------
# Worker-contract enforcement at the implement_tasks report (fb2846dc).
# allowed_files/verify/stop_if (models/task.py) were stored + displayed but
# never checked — a dev-step report could silently touch anything. This
# closes that: a non-empty allowed_files is checked against the REAL
# workspace diff at report time, refusing on an out-of-contract change
# unless the worker explicitly acknowledges it (audited, not a hard wall —
# other tasks' uncommitted work can share this tree).
# ---------------------------------------------------------------------------

def _contract_workspace_root(task_id: str) -> Optional[str]:
    """The SAME per-task workspace-root resolution the oracle receipt's
    tree_sha uses (oracle_spec.current_tree_sha / ConductorService.
    _verify_gate) — never the daemon cwd."""
    try:
        ws = task_workspace.workspace_for(task_id)
    except Exception:
        return None
    return (ws or {}).get("path") or None


def _contract_changed_paths(workspace: str) -> list[str]:
    """Changed + untracked file paths, name-only, relative to `workspace` —
    one `git status --porcelain` pass covers staged, unstaged, AND
    untracked. Never raises: a git failure yields an empty list (the caller
    then sees no offenders, which is the same fail-open behavior an empty
    allowed_files already has — this helper never invents a refusal)."""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    paths: list[str] = []
    for line in r.stdout.splitlines():
        if not line:
            continue
        rest = line[3:] if len(line) > 3 else line.strip()
        if " -> " in rest:  # rename: "old -> new"
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip()
        if rest.startswith('"') and rest.endswith('"'):
            rest = rest[1:-1]
        if rest:
            paths.append(rest)
    return paths


def _contract_norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def _contract_violations(paths: list[str], allowed: list[str]) -> list[str]:
    """Paths not covered by `allowed` — exact match OR a directory-prefix
    match (an allowlisted directory covers everything under it)."""
    allow_norm = [_contract_norm(a) for a in (allowed or [])
                  if str(a or "").strip()]
    offenders = []
    for p in paths:
        pn = _contract_norm(p)
        covered = any(pn == a or pn.startswith(a + "/") for a in allow_norm)
        if not covered:
            offenders.append(p)
    return offenders


def _contract_refusal(svc, task, body: "Ident") -> Optional[dict]:
    """implement_tasks PASS-report guard. Returns a refusal dict to hand
    straight back to the caller, or None to let the report proceed
    (recording an audited acknowledgment first when one covers every
    offender)."""
    allowed = list(getattr(task, "allowed_files", None) or [])
    if not allowed:
        return None  # unconstrained — today's behavior, untouched (AC-4)
    root = _contract_workspace_root(task.id)
    if not root:
        # stop_if: never fall back to the daemon cwd — a structural gap is
        # refused distinctly from an allowlist breach.
        return {"ok": False, "step": "implement_tasks", "advanced": False,
                "reason": "worker-contract check could not resolve this "
                          "task's workspace root server-side — refusing "
                          "rather than falling back to the daemon cwd",
                "next_job": _job(task)}
    offenders = _contract_violations(_contract_changed_paths(root), allowed)
    if not offenders:
        return None
    acked = {_contract_norm(p)
             for p in (body.acknowledged_out_of_contract or [])}
    offenders_norm = {_contract_norm(p) for p in offenders}
    if acked and offenders_norm <= acked:
        try:
            svc._task_svc.record_history(
                task.id, action="worker_contract_ack",
                details=("acknowledged out-of-contract files: "
                          + ", ".join(sorted(offenders))),
                actor=body.session_id or "")
        except Exception:
            pass
        return None
    return {"ok": False, "step": "implement_tasks", "advanced": False,
            "reason": ("worker contract violated: file(s) outside "
                       "allowed_files changed — " + ", ".join(sorted(offenders))
                       + ". Re-send the report with "
                         "acknowledged_out_of_contract=[...] naming these "
                         "paths for an audited exception."),
            "offending_files": sorted(offenders),
            "next_job": _job(task)}


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
            from prism_service.services.conductor_service import MACHINE_SEATS
            producers = [s.get("session_id")
                         for s in svc._task_svc.sessions_for_task(body.task_id)
                         if s.get("session_id") not in MACHINE_SEATS]
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
        # WORKER-CONTRACT ENFORCEMENT (fb2846dc): the implement_tasks PASS
        # report only — never another agent step, never a gate — is checked
        # against the task's own allowed_files (empty = unconstrained).
        if step["id"] == "implement_tasks":
            refusal = _contract_refusal(svc, task, body)
            if refusal is not None:
                return refusal
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
        # MINT RED EVIDENCE at the red step (task a5e8d877): a SUCCESS report
        # on write_failing_tests records the red-step commit and lets the
        # trusted runner demonstrate the pinned tests FAILING there, so the
        # red_gate that follows is machine-decidable on a receipt instead of
        # stranding as 'evidence not on file'. Best-effort like the green
        # mint: an error never blocks the advance.
        if step["id"] == "write_failing_tests":
            try:
                svc.mint_red_evidence(body.task_id,
                                      session_id=body.session_id)
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
