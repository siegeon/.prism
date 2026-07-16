"""Tasks API — kanban list, detail, transitions, history."""

import dataclasses
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from prism_service.data_dir import evidence_dir, prototype_file
from prism_service.project_context import get_project
from prism_service.services.task_service import SESSION_GATE_FIX

router = APIRouter()


def _svc(project: str):
    try:
        return get_project(project).task_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")


def _scores_db(project: str) -> str:
    """scores.db path for the project — same resolution api/agent_runs.py uses
    (ctx._data_dir / 'scores.db'), so the Trace tab reads the agent_runs spine
    the telemetry channel writes."""
    return str(get_project(project)._data_dir / "scores.db")


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
    full_outcome_complete: bool = False
    enter_conductor: bool = False
    # Conductor session gate (ef81fc15): optional driving session linked in
    # the same request right after create (two writes, not one transaction —
    # benign, because the gate re-checks on every transition) — REQUIRED when
    # enter_conductor is true so a task can never be handed to the conductor
    # without a session.
    session_id: Optional[str] = None


@router.post("")
def create_task(body: TaskCreate, project: str = Query("default")) -> dict:
    """Create a task from the SPA, optionally entering it into conductor.

    Backs the /conductor 'create task -> enter conductor' onboarding
    affordance. When enter_conductor is true the new task is immediately
    advanced into the workflow (same path as the MCP conductor_advance tool)
    so it appears on the SDLC swimlanes right away — which is exactly why a
    session must ride along: sessionless conductor tiles are frozen (no
    transcript, no tokens, no live signals). The gate is validated BEFORE
    the row is inserted so a refusal never orphans a task.
    """
    if not (body.title or "").strip():
        raise HTTPException(422, "title is required")
    sid = (body.session_id or "").strip()
    if body.enter_conductor and not sid:
        raise HTTPException(422, SESSION_GATE_FIX)
    ctx = get_project(project)
    task = ctx.task_svc.create(
        title=body.title.strip(),
        description=body.description or "",
        priority=body.priority or 0,
        tags=body.tags or [],
        likely_misfire=body.likely_misfire or "",
        full_outcome_complete=bool(body.full_outcome_complete),
    )
    out: dict = {"task": task, "advanced": None}
    if sid:
        ctx.task_svc.link_session(task.id, sid)
        out["sessions"] = ctx.task_svc.sessions_for_task(task.id)
    if body.enter_conductor:
        cond = getattr(ctx, "conductor_svc", None)
        if cond is not None:
            out["advanced"] = cond.advance_task(
                task.id, validation="entered via SPA onboarding",
                session_id=sid or None,
            )
    return out


class SessionLinkBody(BaseModel):
    session_id: str


@router.post("/{task_id}/sessions")
def link_task_session(
    task_id: str, body: SessionLinkBody, project: str = Query("default"),
) -> dict:
    """REST twin of the MCP `task_link_session` verb (ef81fc15).

    Upserts the same task_sessions(task_id, session_id) row through the
    single TaskService writer — idempotent on re-link (started_at keeps its
    first-observed stamp) — and returns the full linked list so the caller
    can immediately verify the association that unblocks the conductor
    session gate on PATCH -> in_progress.
    """
    svc = _svc(project)
    if svc.get(task_id) is None:
        raise HTTPException(404, "task not found")
    sid = (body.session_id or "").strip()
    if not sid:
        raise HTTPException(422, "session_id is required")
    linked = svc.link_session(task_id, sid)
    return {
        "task_id": task_id,
        "linked": bool(linked),
        "sessions": svc.sessions_for_task(task_id),
    }


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
            pp = cond.phase_progress(task_id)
            out["phase_progress"] = pp
            # Honest work state (working/adrift/stalled/…) rides the detail
            # route alongside phase_progress so the header pill can't lie.
            if hasattr(cond, "activity_for"):
                out["activity"] = cond.activity_for(t, pp)
    except Exception:
        pass
    # has_prototype: a clickable MOCK prototype HTML the /prototype workflow
    # generated for this task, served in-app (see get_task_prototype). Top-level
    # boolean (like phase_progress) so the detail page can show/hide the iframe
    # without a DB column. Best-effort — never break the detail route.
    try:
        out["has_prototype"] = prototype_file(task_id).exists()
    except Exception:
        out["has_prototype"] = False
    return out


@router.get("/{task_id}/trace")
def get_task_trace(task_id: str, project: str = Query("default")) -> dict:
    """Drive-scoped token trace for the task-detail Trace tab.

    Groups this task's agent_runs (the per-agent/step telemetry spine) by
    session then SDLC step, with token counts on every row —
    ``{"sessions": [{session_id, tokens_total, steps: [...]}],
    "totals": {tokens, steps, sessions}}``. A task with no runs returns empty
    arrays so the tab shows an honest empty state rather than 404. Cross-task
    totals belong on the Sessions page; this stays scoped to the one drive."""
    from prism_service.services.agent_runs_data import build_task_trace

    return build_task_trace(_scores_db(project), task_id)


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


# Evidence images a drive cites in its proof — whitelisted image types only,
# so this route can never serve executable content.
_EVIDENCE_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@router.get("/{task_id}/evidence/{filename}")
def get_task_evidence(task_id: str, filename: str):
    """Serve one of the task's evidence files (gate/audit screenshots) so the
    SPA renders it inline where the proof cites it — evidence viewable IN
    PRISM, never an external host (owner 2026-07-16). Both path pieces are
    whitelisted so a crafted request can't traverse out of the evidence dir."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise HTTPException(400, "bad task id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename) or ".." in filename:
        raise HTTPException(400, "bad filename")
    ext = filename[filename.rfind(".") :].lower() if "." in filename else ""
    media = _EVIDENCE_MEDIA.get(ext)
    if not media:
        raise HTTPException(400, "unsupported evidence type")
    path = evidence_dir(task_id) / filename
    if not path.exists():
        raise HTTPException(404, "no such evidence file")
    return FileResponse(str(path), media_type=media)


def _clean_doc(doc: str) -> str:
    """Collapse whitespace and demote RST double-backtick literals so a test
    docstring reads as plain prose in the UI panel."""
    doc = (doc or "").strip()
    doc = re.sub(r"``([^`]+)``", r"\1", doc)
    return " ".join(doc.split())


def _extract_tests_from_source(source: str, rel_file: str) -> list[dict]:
    """AST-parse a test module and return one record per ``def test_*`` with
    its (cleaned) docstring, the test BODY source (so a reviewer can read
    what it actually asserts, not just its name), and its line number.
    Never raises — a syntax error yields []."""
    import ast

    out: list[dict] = []
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except Exception:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            # The function's own source, docstring stripped, capped — enough
            # to EVALUATE the pin (its asserts), not a wall of text.
            body_start = (node.body[0].end_lineno
                          if (node.body and isinstance(node.body[0], ast.Expr)
                              and isinstance(getattr(node.body[0], "value", None), ast.Constant)
                              and isinstance(node.body[0].value.value, str))
                          else node.body[0].lineno - 1 if node.body else node.lineno)
            snippet = "\n".join(
                lines[body_start:(node.end_lineno or body_start)][:24]
            ).strip("\n")
            out.append(
                {
                    "name": node.name,
                    "doc": _clean_doc(ast.get_docstring(node) or ""),
                    "file": rel_file,
                    "line": node.lineno,
                    "snippet": snippet,
                }
            )
    return out


@router.get("/{task_id}/tests")
def get_task_tests(task_id: str, run: bool = Query(False)):
    """Discover the test file(s) that PIN this task's oracle and return each
    ``def test_*`` as ``{name, doc, file}`` so the detail page can show,
    next to the oracle, exactly which tests pin the acceptance criteria.

    Discovery is content-based and read-only: scan ``tests/**/*.py`` for files
    that mention the task id (full or first 8 chars) — the red-test file names
    the task in its module docstring. Best-effort: any parse/read failure is
    swallowed and an empty list is returned. task_id is validated (same shape
    as the prototype route) so a crafted id can't influence the scan.

    ``run=true`` additionally EXECUTES the discovered test files (pytest, in
    the daemon's service checkout, bounded) and stamps each row with its REAL
    current ``status`` (passed/failed/skipped/not-run) — the honest fix for
    the tab painting every pin RED forever: red is a phase, not a badge."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise HTTPException(400, "bad task id")

    from pathlib import Path

    tests_root = Path(__file__).resolve().parents[2] / "tests"
    short = task_id[:8]
    results: list[dict] = []
    files: list[str] = []
    if tests_root.is_dir():
        for p in sorted(tests_root.rglob("*.py")):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            if task_id in text or (len(short) >= 8 and short in text):
                rel = p.relative_to(tests_root.parent).as_posix()
                files.append(rel)
                results.extend(_extract_tests_from_source(text, rel))
    ran = False
    if run and files and results:
        statuses = _run_pinned_tests(tests_root.parent, files)
        if statuses is not None:
            ran = True
            for row in results:
                row["status"] = statuses.get(row["name"], "not-run")
    return {"tests": results, "ran": ran}


def _run_pinned_tests(service_root, files: list[str]) -> Optional[dict]:
    """Run the pinned test files (bounded) and map test name -> outcome via
    pytest's line report. Returns None when the run itself could not happen
    (missing interpreter, timeout) — callers then omit statuses honestly."""
    import subprocess
    import sys as _sys
    try:
        cmd = [_sys.executable, "-m", "pytest", "-v", "--no-header",
               "--color=no", "-p", "no:cacheprovider", *files]
        out = subprocess.run(
            cmd, cwd=str(service_root), capture_output=True, text=True,
            timeout=180)
        statuses: dict[str, str] = {}
        for line in (out.stdout or "").splitlines():
            m = re.match(r".*::(\w+)(?:\[[^\]]*\])?\s+(PASSED|FAILED|ERROR|"
                         r"SKIPPED|XFAIL|XPASS)", line)
            if m:
                statuses[m.group(1)] = m.group(2).lower()
        return statuses
    except Exception:
        return None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[int] = None
    # tags were settable at create but never updatable over the API — the
    # control-plane's 'policy-change' authorized route was unreachable for
    # SPA/API users (2026-07-16).
    tags: Optional[list[str]] = None
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
    # Conductor session gate (ef81fc15): flipping a task to in_progress
    # hands it to the conductor (intake lane on /conductor). Refuse the
    # TRANSITION when no session is linked — this exact sessionless PATCH
    # produced 5 frozen conductor tiles on the live board. Grandfathered
    # rows already in_progress are untouched (same-status PATCHes and
    # non-status fields pass through).
    if kwargs.get("status") == "in_progress":
        current = svc.get(task_id)
        if current is None:
            raise HTTPException(404, "task not found")
        if current.status != "in_progress" and not svc.sessions_for_task(task_id):
            raise HTTPException(422, SESSION_GATE_FIX)
    t = svc.update(task_id, **kwargs)
    if not t:
        raise HTTPException(404, "task not found")
    return {"task": t, "history": svc.history(task_id)}
