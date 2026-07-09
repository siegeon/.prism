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
    return {
        "task_id": task.id,
        "step": step["id"],
        "kind": kind,
        "role": step.get("agent") or "",
        "role_label": _ROLE.get(step.get("agent") or "", "-"),
        "gate_state": task.gate_state,
        "instructions": (
            "GATE: an independent, DISTINCT actor decides."
            if kind == "gate" else _GUIDE.get(step["id"], step["id"])
        ),
        "expected_proof": step.get("validation") or (
            "prior step's validation" if kind == "gate" else "n/a"),
    }


class Ident(BaseModel):
    task_id: str
    session_id: Optional[str] = None
    outcome: object = ""
    model: Optional[str] = None
    # Gates enforce for REAL by default (rubric for story/plan, verifier +
    # artifact for red/green). override is an explicit, audited exception —
    # a genuine independent reviewer forcing a decision — never the default.
    override: bool = False


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
    if not task.workflow_step:
        svc.advance_task(body.task_id, session_id=body.session_id,
                         model=body.model)
        task = svc._task_svc.get(body.task_id)
    return {"ok": True, "job": _job(task)}


@router.post("/report")
def flow_report(body: Ident, project: str = Query("default")) -> dict:
    """Record the worker's outcome and let the SERVER advance the flow."""
    svc = _svc(project)
    task = svc._task_svc.get(body.task_id)
    if task is None:
        return {"ok": False, "error": "unknown task"}
    step = ConductorService._step_by_id(task.workflow_step)
    if step is None:
        return {"ok": False, "error": "task has not started the flow"}

    if step["type"] == "gate":
        # Distinct-actor, enforced up front by session identity: a worker
        # who produced any prior step of this task may not clear its gate.
        producers = []
        try:
            producers = [s.get("session_id")
                         for s in svc._task_svc.sessions_for_task(body.task_id)]
        except Exception:
            producers = []
        if body.session_id and body.session_id in producers:
            return {"ok": False, "step": step["id"],
                    "reason": "distinct-actor: the producing session cannot "
                              "clear its own gate — route to a distinct worker "
                              "(or the claude -p adjudicator)",
                    "producers": producers}
        # Approve on MERIT: no blanket override. story/plan are scored by
        # their YAML rubric (plan_doc/plan_diagram); red/green run the
        # verifier + the proof-carrying artifact tooth. A fabricated or
        # missing proof is refused here, exactly as it should be.
        res = svc.gate_decide(body.task_id, "approve",
                              reason=str(body.outcome) or "flow approval",
                              override=body.override, session_id=body.session_id,
                              model=body.model)
    else:
        res = svc.advance_task(body.task_id, session_id=body.session_id,
                              model=body.model)

    nxt = svc._task_svc.get(body.task_id)
    return {"ok": res.get("ok", False), "advanced": res,
            "next_job": _job(nxt)}
