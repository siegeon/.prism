"""Tasks API — kanban list, detail, transitions, history."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from prism_service.project_context import get_project

router = APIRouter()


def _svc(project: str):
    try:
        return get_project(project).task_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")


def _attach_turn_tokens(history: list, sessions: list, project: str) -> None:
    """Best-effort: stamp each history turn with `turn_tokens` = the
    output_tokens spent in the window (previous_turn, this_turn], summed across
    the task's linked-session transcripts. The detail-page timeline pairs this
    with the same window's elapsed gap. Never raises — a missing transcript /
    folder-mode-off project simply leaves the field unset (UI hides it)."""
    if not history:
        return
    try:
        from datetime import datetime

        from prism_service.services.claude_transcripts import (
            _project_source_path,
            live_token_events_for_session,
        )

        src = _project_source_path(project)
        if not src:
            return

        def _epoch(ts: str) -> float | None:
            try:
                return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                return None

        # Sorted turn boundaries (skip rows with an unparseable timestamp).
        bounds = [(_epoch(h.get("timestamp", "")), i) for i, h in enumerate(history)]
        bounds = [b for b in bounds if b[0] is not None]
        if len(bounds) < 2:
            return
        bounds.sort(key=lambda b: b[0])

        events: list[tuple[float, int]] = []
        for s in sessions or []:
            sid = s.get("session_id") if isinstance(s, dict) else getattr(s, "session_id", "")
            if sid:
                events.extend(live_token_events_for_session(sid, src))
        if not events:
            return

        import bisect

        edges = [b[0] for b in bounds]
        sums = [0] * len(history)
        for ev_ts, tok in events:
            # window (edges[k-1], edges[k]] -> the turn that closed it (bounds[k]).
            k = bisect.bisect_left(edges, ev_ts)
            if 1 <= k < len(bounds):
                sums[bounds[k][1]] += tok
        for i, h in enumerate(history):
            if sums[i] > 0 and isinstance(h, dict):
                h["turn_tokens"] = sums[i]
    except Exception:
        pass


@router.get("")
def list_tasks(project: str = Query("default")) -> dict:
    return {"tasks": _svc(project).list()}


@router.get("/next")
def next_task(project: str = Query("default")) -> dict:
    return {"next": _svc(project).next_task()}


@router.get("/{task_id}")
def get_task(task_id: str, project: str = Query("default")) -> dict:
    svc = _svc(project)
    t = svc.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    # `sessions` rides the EXISTING task-detail route (no parallel
    # top-level route) — the linked Claude sessions JOINed with their
    # session_outcomes metrics. Empty list when nothing is linked.
    out = {
        "task": t,
        "history": svc.history(task_id),
        "sessions": svc.sessions_for_task(task_id),
    }
    # Attribute per-turn token spend (output_tokens bucketed into each turn's
    # (prev, this] wall-clock window) onto the history rows. Best-effort.
    _attach_turn_tokens(out["history"], out["sessions"], project)
    # phase_progress (a5e0d9f5): the animated SDLC bar in the detail header
    # reads the blended current-step fill. Best-effort — never break the
    # detail route if conductor is unavailable.
    try:
        cond = get_project(project).conductor_svc
        if cond is not None and hasattr(cond, "phase_progress"):
            out["phase_progress"] = cond.phase_progress(task_id)
    except Exception:
        pass
    return out


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    priority: Optional[int] = None
    assigned_agent: Optional[str] = None
    blocked_reason: Optional[str] = None
    parent_id: Optional[str] = None
    oracle: Optional[str] = None
    proof_type: Optional[str] = None
    completion_proof: Optional[str] = None
    allowed_files: Optional[list[str]] = None
    verify: Optional[list[str]] = None
    stop_if: Optional[list[str]] = None
    plan_doc: Optional[str] = None
    plan_diagram: Optional[str] = None


@router.patch("/{task_id}")
def update_task(
    task_id: str, body: TaskUpdate, project: str = Query("default"),
) -> dict:
    """v6.0.9 — SPA-side task transitions. Mirrors mcp__prism__task_update
    so users can flip a card from the kanban without spawning a Claude
    session for one-line edits."""
    svc = _svc(project)
    kwargs = {k: v for k, v in body.dict().items() if v is not None}
    if not kwargs:
        raise HTTPException(400, "no fields to update")
    t = svc.update(task_id, **kwargs)
    if not t:
        raise HTTPException(404, "task not found")
    return {"task": t, "history": svc.history(task_id)}
