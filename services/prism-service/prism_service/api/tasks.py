"""Tasks API — kanban list, detail, transitions, history."""

import dataclasses
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from prism_service.data_dir import prototype_file
from prism_service.project_context import get_project

router = APIRouter()


def _svc(project: str):
    try:
        return get_project(project).task_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")


def _call_svc(fn, *args, **kwargs):
    """Invoke a TaskService mutation, translating its boundary-validation
    ValueError (off-enum status, over-cap title, bad parent — task
    16234231/24ed8027) into the HTTP 422 contract the SPA/API expects."""
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


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
            project_token_events_in_window,
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
        fallback = False
        if not events:
            # No authoritative linked-session spend (the task↔session link was
            # never written, or the linked id maps to no transcript). Fall back
            # to wall-clock attribution across the project's transcripts so the
            # timeline still shows real per-turn tokens — the work is on disk
            # regardless of the link. Bounded to the task's turn span.
            events = project_token_events_in_window(src, bounds[0][0], bounds[-1][0])
            fallback = True
        if not events:
            return

        import bisect

        edges = [b[0] for b in bounds]
        sums = [0] * len(history)
        # Fallback attribution gets an idle clamp: a turn that closed a long
        # gap (the task was parked, not worked) must not vacuum up the spend
        # other tasks burned during that gap. 600s mirrors the conductor
        # burn-graph's idle/skew threshold. Linked-session events are this
        # task's own spend, so they skip the clamp.
        IDLE_CLAMP_S = 600.0
        for ev_ts, tok in events:
            # window (edges[k-1], edges[k]] -> the turn that closed it (bounds[k]).
            k = bisect.bisect_left(edges, ev_ts)
            if 1 <= k < len(bounds):
                if fallback and (edges[k] - ev_ts) > IDLE_CLAMP_S:
                    continue
                sums[bounds[k][1]] += tok
        for i, h in enumerate(history):
            if sums[i] > 0 and isinstance(h, dict):
                h["turn_tokens"] = sums[i]
    except Exception:
        pass


@router.get("")
def list_tasks(project: str = Query("default")) -> dict:
    return {"tasks": _svc(project).list()}


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    priority: int = 0
    tags: Optional[list[str]] = None
    likely_misfire: str = ""
    oracle: str = ""
    proof_type: str = ""
    parent_id: str = ""
    dependencies: Optional[list[str]] = None
    full_outcome_complete: bool = False
    enter_conductor: bool = False


@router.post("")
def create_task(body: TaskCreate, project: str = Query("default")) -> dict:
    """Create a task from the SPA, optionally entering it into conductor.

    Backs the /conductor 'create task -> enter conductor' onboarding
    affordance. When enter_conductor is true the new task is immediately
    advanced into the workflow (same path as the MCP conductor_advance tool)
    so it appears on the SDLC swimlanes right away.
    """
    if not (body.title or "").strip():
        raise HTTPException(422, "title is required")
    ctx = get_project(project)
    task = _call_svc(
        ctx.task_svc.create,
        title=body.title.strip(),
        description=body.description or "",
        priority=body.priority or 0,
        tags=body.tags or [],
        likely_misfire=body.likely_misfire or "",
        oracle=body.oracle or "",
        proof_type=body.proof_type or "",
        parent_id=body.parent_id or "",
        dependencies=body.dependencies or [],
        full_outcome_complete=bool(body.full_outcome_complete),
    )
    advanced = None
    if body.enter_conductor:
        cond = getattr(ctx, "conductor_svc", None)
        if cond is not None:
            advanced = cond.advance_task(task.id, validation="entered via SPA onboarding")
    return {"task": task, "advanced": advanced}


@router.get("/next")
def next_task(project: str = Query("default")) -> dict:
    return {"next": _svc(project).next_task()}


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _build_timeline(history: list, sessions: list) -> dict:
    """Typed activity timeline backing the task-detail Gantt (Way 1).

    Two row kinds, deliberately NOT one-size-fits-all:
      * lanes  — REAL transcript-backed work sessions (UUID id), positioned as
        wall-time bars. Synthetic gate-actor labels (qa-red-gate-*, *-verifier-*)
        are EXCLUDED here — they were never sessions; they surface as gate
        markers instead. This is the fix for the bare-row leak.
      * gates  — one marker per gate RESOLUTION (the deciding gate_decide row),
        carrying honesty (real-verifier vs override), actor, and proof summary.
    """
    from datetime import datetime, timezone

    def _epoch(ts: object) -> float | None:
        try:
            return datetime.fromisoformat(
                str(ts).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None

    now = datetime.now(timezone.utc).timestamp()
    hstamps = [e for e in (_epoch(h.get("timestamp")) for h in history
                           if isinstance(h, dict)) if e is not None]
    start = min(hstamps) if hstamps else now
    end = max(now, max(hstamps) if hstamps else now)

    lanes = []
    for s in sessions or []:
        sid = s.get("session_id", "") if isinstance(s, dict) else ""
        if not _UUID_RE.match(sid or ""):
            continue  # synthetic actor label -> not a work session lane
        st = _epoch(s.get("started_at")) or start
        dur = float(s.get("duration_s") or 0)
        en = _epoch(s.get("ended_at")) or (st + dur if dur else end)
        lanes.append({
            "session_id": sid, "start": st, "end": max(en, st),
            "duration_s": s.get("duration_s") or 0,
            "skills": s.get("skills_invoked") or 0,
            "live": s.get("ended_at") is None,
        })

    gates = []
    for h in history:
        if not isinstance(h, dict) or h.get("action") != "gate_decide":
            continue
        det = str(h.get("details") or "")
        # Skip the intermediate REJECTED attempt (verifier=fail, not yet
        # overridden) — show only the row that RESOLVED the gate.
        if "verifier=fail" in det and "override=True" not in det:
            continue
        ts = _epoch(h.get("timestamp"))
        if ts is None:
            continue
        gm = re.search(r"gate=(\w+?)_gate", det)
        gate = gm.group(1) if gm else "gate"
        override = ("override=True" in det
                    or (h.get("actor") or "") == "manual-override")
        am = re.search(r"override-actor=([^\s;]+)", det)
        actor = am.group(1) if am else (h.get("actor") or "")
        # Real suite result needs 2+ digits so "0/1 pass" can't masquerade as a
        # green suite; red gates carry a "trace" proof instead.
        pm = re.search(r"(\d{2,})\s+passed", det)
        proof = (f"suite {pm.group(1)}✓" if pm
                 else "trace" if "trace" in det.lower() else "")
        gates.append({
            "gate": gate, "ts": ts, "actor": actor, "override": override,
            "verified": not override, "proof": proof, "reason": det[:200],
        })

    return {"window": {"start": start, "end": end},
            "lanes": lanes, "gates": gates}


def _build_child_map(all_tasks: list) -> dict:
    """parent_id → [child Task] index built from one task-list read."""
    m: dict = {}
    for t in all_tasks:
        pid = getattr(t, "parent_id", "") or ""
        if pid:
            m.setdefault(pid, []).append(t)
    return m


def _subtree_nodes(child_map: dict, root_id: str) -> list:
    """Breadth-first list of every transitive child of ``root_id`` (root
    excluded), cycle-guarded via a visited-set (mirrors TaskService.descendants
    — kept here so get_task walks its single in-memory map, no extra reads)."""
    out: list = []
    seen = {root_id}
    queue = list(child_map.get(root_id, []))
    while queue:
        n = queue.pop(0)
        if n.id in seen:
            continue
        seen.add(n.id)
        out.append(n)
        queue.extend(child_map.get(n.id, []))
    return out


@router.get("/{task_id}")
def get_task(task_id: str, project: str = Query("default")) -> dict:
    svc = _svc(project)
    t = svc.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    # history() yields TaskHistory dataclasses — convert to plain dicts here so
    # _attach_turn_tokens can read timestamps and stamp `turn_tokens` (a
    # dataclass has no .get/__setitem__, and a setattr'd non-field attribute
    # would be dropped by jsonable_encoder's asdict). This is why per-turn
    # token pills never surfaced before — the attach silently AttributeError'd.
    history = [
        dataclasses.asdict(h) if dataclasses.is_dataclass(h) else dict(h)
        for h in svc.history(task_id)
    ]
    # `sessions` rides the EXISTING task-detail route (no parallel
    # top-level route) — the linked Claude sessions JOINed with their
    # session_outcomes metrics. Empty list when nothing is linked.
    out = {
        "task": t,
        "history": history,
        "sessions": svc.sessions_for_task(task_id),
    }
    # Attribute per-turn token spend (output_tokens bucketed into each turn's
    # (prev, this] wall-clock window) onto the history rows. Best-effort.
    _attach_turn_tokens(out["history"], out["sessions"], project)
    # Way 1 — typed activity Gantt (real session lanes + gate markers).
    out["timeline"] = _build_timeline(out["history"], out["sessions"])
    # phase_progress (a5e0d9f5): the animated SDLC bar in the detail header
    # reads the blended current-step fill. Best-effort — never break the
    # detail route if conductor is unavailable.
    try:
        cond = get_project(project).conductor_svc
        if cond is not None and hasattr(cond, "phase_progress"):
            out["phase_progress"] = cond.phase_progress(task_id)
    except Exception:
        pass
    # pi_activity (task fc08da8d): the PI-panel agent's task-attributed runs
    # from the pi_runs ledger (model, tokens, ms, tools_used) — the detail
    # page renders a "PI agent" activity row so a task the browser PI agent
    # drove shows its token+tool usage, not just Claude sessions. Best-effort.
    try:
        from prism_service.services import pi_run_log
        out["pi_activity"] = pi_run_log.list_recent(
            limit=50, project=project, task_id=task_id)
    except Exception:
        out["pi_activity"] = []
    # has_prototype: a clickable MOCK prototype HTML the /prototype workflow
    # generated for this task, served in-app (see get_task_prototype). Top-level
    # boolean (like phase_progress) so the detail page can show/hide the iframe
    # without a DB column. Best-effort — never break the detail route.
    try:
        out["has_prototype"] = prototype_file(task_id).exists()
    except Exception:
        out["has_prototype"] = False
    # Subtree roll-up (task e72aba6d): whole-subtree descendant_count/done for
    # the detail page's tree header, plus each DIRECT child annotated with its
    # OWN child_count so the UI can lazy-render a tree node's chevron without
    # re-walking. One task-list read backs all three (built into child_map).
    try:
        child_map = _build_child_map(svc.list())
        subtree = _subtree_nodes(child_map, task_id)
        active = [t for t in subtree if getattr(t, "status", "") != "cancelled"]
        out["descendant_count"] = len(active)
        out["descendant_done"] = sum(
            1 for t in active if getattr(t, "status", "") == "done")
        out["children"] = [
            {**dataclasses.asdict(c),
             "child_count": len(_subtree_nodes(child_map, c.id))}
            for c in child_map.get(task_id, [])
        ]
    except Exception:
        out.setdefault("descendant_count", 0)
        out.setdefault("descendant_done", 0)
        out.setdefault("children", [])
    return out


@router.get("/{task_id}/prototype")
def get_task_prototype(task_id: str):
    """Serve the task's prototype HTML so the SPA can iframe it on the Plan
    card (prototypes viewable IN PRISM, not an external port). task_id is a
    server-generated UUID; reject anything else so a crafted id can't traverse
    out of the prototypes dir."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise HTTPException(400, "bad task id")
    path = prototype_file(task_id)
    if not path.exists():
        raise HTTPException(404, "no prototype for this task")
    return FileResponse(str(path), media_type="text/html")


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[int] = None
    assigned_agent: Optional[str] = None
    blocked_reason: Optional[str] = None
    parent_id: Optional[str] = None
    oracle: Optional[str] = None
    proof_type: Optional[str] = None
    completion_proof: Optional[str] = None
    likely_misfire: Optional[str] = None
    full_outcome_complete: Optional[bool] = None
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
    t = _call_svc(svc.update, task_id, **kwargs)
    if not t:
        raise HTTPException(404, "task not found")
    return {"task": t, "history": svc.history(task_id)}
