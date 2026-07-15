"""Conductor API — prompt variants, scores, session outcomes, and SDLC state."""

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from prism_service.project_context import get_project
from prism_service.services.conductor_service import board_health

router = APIRouter()


def _svc(project: str):
    try:
        return get_project(project).conductor_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")


@router.get("/state")
def state(project: str = Query("default"), outcomes_limit: int = Query(200, ge=1, le=1000)) -> dict:
    s = _svc(project)
    return {
        "exploration_rate": s.exploration_rate(),
        "variants": s.get_variants(),
        "scores": s.get_scores(),
        "session_outcomes": s.get_session_outcomes(limit=outcomes_limit),
        "retired": s.get_retired(),
        # Conductor v2 (#79 follow-up): SPA /conductor page reads these to
        # render the SDLC dashboard — which tasks conductor is driving and
        # where they are in the workflow.
        "managed_tasks": s.managed_tasks(),
        "step_buckets": s.step_buckets(),
        # GoalBuddy GAP-5: cross-task "reorient" signal composed from the
        # per-task '⚠' advisory notes — fires when >= 2 low-value done-root
        # completions lead the newest-first run. Additive; SPA renders a badge.
        "board_health": board_health(_board_tasks(s)),
    }


def _board_tasks(s) -> list:
    """All tasks for the board_health scan (done-root filtering happens there)."""
    svc = getattr(s, "_task_svc", None)
    if svc is None:
        return []
    try:
        return svc.list()
    except Exception:
        return []


@router.post("/gate/mint")
def gate_mint(task_id: str = Body(..., embed=True),
              project: str = Query("default")) -> dict:
    """Re-run the oracle and MINT a fresh EvidenceReceipt from INSIDE the
    daemon — the same process whose pinned-policy resolution the gate's
    approve check uses, so a receipt minted here is fresh BY CONSTRUCTION
    (out-of-process mints hit cwd-dependent pin skew — defect 68e5c699).
    This is the gate card's 'Re-run oracle' action."""
    s = _svc(project)
    res = s.mint_green_evidence(task_id, session_id="gate-card-rerun",
                                model="daemon")
    task = s._task_svc.get(task_id) if s._task_svc else None
    refusal = ""
    if task is not None:
        refusal, _ = s._oracle_receipt_refusal(task, override=False, reason="")
    return {"ok": bool(res.get("ok")), "reason": res.get("reason", ""),
            "receipt_ok": not refusal, "receipt_refusal": refusal or ""}


@router.get("/gate/readiness")
def gate_readiness(task_id: str, project: str = Query("default")) -> dict:
    """LIVE gate-card truth (owner 2026-07-14: the stored gate_reason is a
    stale snapshot that contradicts reality and gives no action). Checks the
    CHEAP evidence teeth at request time — never a cached decision string:
      - receipt: the oracle EvidenceReceipt tooth exactly as the gate's
        approve path evaluates it (fresh tree + spec + policy), read-only.
    Returns what a plain Approve would consult, so the card can say
    'Approve will pass the evidence tooth' vs 'evidence missing: <why>'."""
    s = _svc(project)
    task = getattr(s, "_task_svc", None) and s._task_svc.get(task_id)
    if task is None:
        raise HTTPException(404, "unknown task")
    refusal, receipt = s._oracle_receipt_refusal(
        task, override=False, reason="")
    out: dict = {
        "receipt_ok": not refusal,
        "receipt_refusal": refusal or "",
    }
    if receipt is not None:
        out["receipt"] = {
            "adapter": getattr(receipt, "adapter", ""),
            "passed": bool(getattr(receipt, "passed", False)),
            "status": getattr(receipt, "status", ""),
            "ended_at": getattr(receipt, "ended_at", ""),
            "reason": str(getattr(receipt, "reason", ""))[:300],
        }
    else:
        try:
            from prism_service.services import oracle_spec as osp
            latest = osp.latest_receipt(project, task_id)
            if latest is not None:
                out["receipt"] = {
                    "adapter": latest.adapter,
                    "passed": bool(latest.passed),
                    "status": latest.status,
                    "ended_at": latest.ended_at,
                    "reason": str(latest.reason)[:300],
                }
        except Exception:
            pass
    return out


@router.post("/gate")
def gate(project: str = Query("default"), body: dict = Body(...)) -> dict:
    """Resolve a pending conductor gate from the SPA.

    Same path as the MCP `conductor_gate` tool: delegates to
    ConductorService.gate_decide. approve flips gate_state->'passed' and
    auto-advances; reject flips->'failed' and stores reason in
    task.gate_reason. The decision PERSISTS on the task row.
    """
    s = _svc(project)
    task_id = body.get("task_id")
    action = body.get("action")
    if not task_id or action not in ("approve", "reject"):
        raise HTTPException(422, "task_id and action ('approve'|'reject') required")
    return s.gate_decide(
        task_id,
        action,
        reason=body.get("reason", "") or "",
        override=bool(body.get("override", False)),
        session_id=body.get("session_id"),
        # NO-SELF-OVERRIDE actor (task 3826dac3): defaults to the session.
        actor=body.get("actor") or body.get("session_id"),
    )


class RewindBody(BaseModel):
    task_id: str
    reason: str = ""
    actor: str = "owner"


@router.post("/rewind")
def rewind(project: str = Query("default"),
           body: RewindBody = Body(...)) -> dict:
    """AUDITED one-step rewind for an overshot task — the owner/admin repair
    lever for a double-advanced drive (task b07fd46e). Delegates to
    ConductorService.rewind_task; a blank reason is refused there (the lever
    is guarded, never a silent hand-drive)."""
    s = _svc(project)
    return s.rewind_task(body.task_id, reason=body.reason, actor=body.actor)


class FanoutBody(BaseModel):
    task_id: str
    step: str
    dispatched: int = 0
    returned: int = 0


@router.post("/fanout")
def fanout(project: str = Query("default"), body: FanoutBody = Body(...)) -> dict:
    """Record per-step sub-agent fanout (dispatched vs returned) for the SPA.

    Ephemeral units — e.g. "8 test-writers handed out, 8 back" — for the
    CURRENT workflow step; distinct from phase_progress' CHILD-TASK basis.
    UPSERTs on (task_id, step) and returns the stored row.
    """
    s = _svc(project)
    return s.set_step_fanout(body.task_id, body.step, body.dispatched, body.returned)


@router.post("/advance")
def advance(project: str = Query("default"), body: dict = Body(...)) -> dict:
    """Advance a task to the next WORKFLOW_STEP (same as the MCP advance)."""
    s = _svc(project)
    task_id = body.get("task_id")
    if not task_id:
        raise HTTPException(422, "task_id required")
    # Conductor session gate (ef81fc15): ENTERING the workflow (step '' ->
    # step 0) hands the task to the conductor. Refuse a sessionless entry —
    # either the call carries a session_id (stamped by advance_task) or the
    # task already has a linked session. Mid-workflow advances are not
    # entry transitions and pass through (grandfathered drives keep moving).
    task_svc = getattr(s, "_task_svc", None)
    if task_svc is not None:
        t = task_svc.get(task_id)
        if (t is not None and not (getattr(t, "workflow_step", "") or "")
                and not str(body.get("session_id") or "").strip()
                and not task_svc.sessions_for_task(task_id)):
            from prism_service.services.task_service import SESSION_GATE_FIX
            raise HTTPException(422, SESSION_GATE_FIX)
    out = s.advance_task(
        task_id,
        validation=body.get("validation"),
        session_id=body.get("session_id"),
    )
    # Normalize key for the SPA: advance_task returns 'to_step'.
    return out
