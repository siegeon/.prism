"""Tasks API — kanban list, detail, transitions, history."""

import dataclasses
import re
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from prism_service.api.auth import authorize_project_request
from prism_service.data_dir import evidence_dir, prototype_file
from prism_service.project_context import get_project
from prism_service.services.task_service import SESSION_GATE_FIX

router = APIRouter(dependencies=[Depends(authorize_project_request)])


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


# The board never renders raw `description` (task 842248bd: it is 22% of a
# 1.84 MB board payload) but ONE thing inside it IS rendered indirectly — a
# mirrored/imported task's link-out badge, which regexes the trailing
# provenance URL work_item_sync.py appends. Mirroring the frontend's
# MIRROR_URL_RE (TasksPage.tsx) so a `fields=` projection can ship a small
# derived `mirror_url` instead of the whole body.
_MIRROR_URL_RE = re.compile(r"(https://(?:github\.com|[\w.-]+\.atlassian\.net)/\S+)")


def _mirror_url(description: str) -> str:
    m = _MIRROR_URL_RE.search(description or "")
    return m.group(1) if m else ""


@router.get("")
def list_tasks(project: str = Query("default"),
              include_deleted: bool = Query(False),
              parent_id: Optional[str] = Query(
                  None, description="Scope to one epic's direct children "
                                     "(or '' for root tasks only)."),
              fields: Optional[str] = Query(
                  None, description="Comma-separated projection (task "
                                     "842248bd, FR-7 parity with the MCP "
                                     "task_list tool) - lean board rows "
                                     "instead of the full record. "
                                     "'mirror_url' is a derived field, not a "
                                     "raw Task attribute.")) -> dict:
    # Soft-deleted tasks stay in the store for audit but must NOT surface on
    # the board / task list — a deleted task is removed, not active work (it
    # was still feeding the AWAITING-REVIEW bar and the kanban). Opt in with
    # ?include_deleted=true for an admin/audit view.
    tasks = _svc(project).list(parent_id=parent_id) if parent_id is not None else _svc(project).list()
    if not include_deleted:
        tasks = [t for t in tasks
                 if str(getattr(t, "status", "") or "") != "deleted"]
    if fields:
        field_list = [f.strip() for f in fields.split(",") if f.strip()]
        rows = []
        for t in tasks:
            row = {}
            for f in field_list:
                row[f] = _mirror_url(getattr(t, "description", "") or "") if f == "mirror_url" else getattr(t, f, None)
            rows.append(row)
        return {"tasks": rows}
    return {"tasks": tasks}


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    priority: int = 0
    tags: Optional[list[str]] = None
    likely_misfire: str = ""
    full_outcome_complete: bool = False
    enter_conductor: bool = False
    # REST parity with MCP task_create (task b78a193c): the oracle/proof
    # contract was create-only on MCP — POST /api/tasks silently dropped
    # these fields. Forwarded to task_svc.create() below and validated
    # identically (validate_for_authoring) before the row is inserted.
    oracle: str = ""
    proof_type: str = ""
    verify: Optional[list[str]] = None
    allowed_files: Optional[list[str]] = None
    stop_if: Optional[list[str]] = None
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
    # Authoring-time oracle validation (task b78a193c) — REST mirror of the
    # MCP task_create check: reject the hard contradiction (proof_type=test
    # with an oracle but no runnable pytest ids in verify[]) with the same
    # domain-level message, BEFORE the row is inserted.
    from prism_service.services.oracle_authoring import validate_for_authoring
    spec_summary, domain_errors = validate_for_authoring(
        oracle=body.oracle, proof_type=body.proof_type,
        verify=body.verify, likely_misfire=body.likely_misfire,
        # allowed_files feeds the unwired-slice check (task 597839d9): a slice
        # that creates a module nothing constructs is refused here, not
        # discovered after the epic ships green and dead.
        allowed_files=body.allowed_files,
    )
    if domain_errors:
        raise HTTPException(422, domain_errors[0])
    ctx = get_project(project)
    task = ctx.task_svc.create(
        title=body.title.strip(),
        description=body.description or "",
        priority=body.priority or 0,
        tags=body.tags or [],
        likely_misfire=body.likely_misfire or "",
        full_outcome_complete=bool(body.full_outcome_complete),
        oracle=body.oracle or "",
        proof_type=body.proof_type or "",
        verify=body.verify,
        allowed_files=body.allowed_files,
        stop_if=body.stop_if,
    )
    out: dict = {"task": task, "advanced": None}
    if spec_summary is not None:
        out["oracle_spec"] = spec_summary
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


def _is_shipped_on_main(repo: str, task_id: str) -> bool:
    """Squash-safe shipped-ness: does a commit MESSAGE on origin/main carry
    the `[task:<id8>]` trailer? Reads commit messages on origin/main
    directly, so a squash-merged commit (which shares no parent chain with
    the task's own branch tip -- a sibling, not a descendant) still counts
    as shipped.

    Deliberately NOT a SHA parent-chain walk: that is what
    `get_task_delivery`'s existing "merged" stage uses (tasks.py:591) and it
    false-negatives on exactly this squash case. Fixing that endpoint is
    task 499ba9c9's job, not this helper's -- this helper must not inherit
    that bug, and must not be mistaken for fixing it."""
    needle = f"[task:{task_id[:8]}]"
    rc, out = _git(repo, "log", "--fixed-strings", "--grep", needle,
                   "-n", "1", "--format=%H", "origin/main")
    return rc == 0 and bool(out)


@router.get("/stranded")
def get_stranded_work(project: str = Query("default")) -> dict:
    """DONE means SHIPPED (owner 2026-07-16) -- merged and validated on
    main, not merely gate-passed. Scans every status=done task and reports
    the ones whose [task:<id8>] trailer is not (yet) reachable on
    origin/main, so a "done" task stuck on an unmerged branch surfaces on
    the Dashboard instead of silently vanishing."""
    rows: list[dict] = []
    repo = ""
    try:
        from prism_service.services.claude_transcripts import _project_source_path
        repo = _project_source_path(project) or ""
    except Exception:
        repo = ""
    if repo and _git(repo, "rev-parse", "--verify", "-q", "origin/main")[0] == 0:
        for t in _svc(project).list(status="done"):
            tid = str(getattr(t, "id", "") or "")
            if not tid or _is_shipped_on_main(repo, tid):
                continue
            branch, rev = _resolve_delivery_branch(repo, tid)
            _, count_s = _git(repo, "rev-list", "--count", f"origin/main..{rev}")
            try:
                commits_ahead = int(count_s)
            except ValueError:
                commits_ahead = 0
            branch_on_origin = bool(branch) and _git(
                repo, "rev-parse", "--verify", "-q", f"origin/{branch}")[0] == 0
            rows.append({
                "task_id": tid,
                "title": str(getattr(t, "title", "") or ""),
                "commits_ahead": commits_ahead,
                "branch_on_origin": branch_on_origin,
                "state": "pushed_unmerged" if branch_on_origin else "local_only",
            })
    return {"stranded": rows}


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
def get_task(task_id: str, project: str = Query("default"),
             include_history: bool = Query(False)) -> dict:
    svc = _svc(project)
    t = svc.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    # Task 93d6c6f3 / f77d3e94 (commit 2e7c816, reused here — not
    # reimplemented, AC-6): `history` (per-turn token attach + spend) was
    # ~93% of a measured 130KB detail payload. Mirrors the include_outcomes
    # opt-in already shipped on api/conductor.py:19-48 (task d5465a25):
    # `timeline`/`sessions` stay unconditional (test_task_activity_gantt.py
    # pins timeline as always-present), only the raw history rows + the
    # per-turn-token/spend attach work that depends on them gate on opt-in.
    # TaskDetailPage.tsx's load() now explicitly passes include_history=true
    # (AC-7/NFR-1) so this page's own views are unchanged; other callers
    # (the board list, SSE) never asked for history and now skip the cost.
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
        "history": history if include_history else [],
        "sessions": svc.sessions_for_task(task_id),
    }
    if include_history:
        # Attribute per-turn token spend (output_tokens bucketed into each
        # turn's (prev, this] wall-clock window) onto the history rows.
        # Best-effort. Skipped by default — this is the heavy per-transcript
        # work the opt-in exists to avoid.
        _attach_turn_tokens(history, out["sessions"], project)
        # Honest per-field, per-turn-model dollar spend, split main-vs-background
        # (SpendPanel on the task-detail page). Best-effort.
        try:
            _attach_spend(out, out["sessions"], project)
        except Exception:
            pass
    # Way 1 — typed activity Gantt (real session lanes + gate markers).
    # Always built from the full (unattached) `history` list regardless of
    # include_history — it's a compact derived summary, not the raw rows.
    out["timeline"] = _build_timeline(history, out["sessions"])
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
    totals belong on the Sessions page; this stays scoped to the one drive.

    source_path/override_dir resolve exactly the way _attach_turn_tokens
    resolves them, so a zero-token UUID session gets re-attributed from its
    transcript instead of rendering a dishonest 0."""
    from prism_service.services.agent_runs_data import build_task_trace

    if _svc(project).get(task_id) is None:
        raise HTTPException(404, "task not found")

    src, override = "", ""
    try:
        from prism_service.services.claude_transcripts import _project_source_path
        src = _project_source_path(project) or ""
    except Exception:
        pass
    try:
        from prism_service.services import claude_memory
        override = claude_memory.configured_project_dir(project) or ""
    except Exception:
        pass
    return build_task_trace(
        _scores_db(project), task_id, source_path=src, override_dir=override
    )


def _attach_spend(out: dict, sessions: list, project: str) -> None:
    """Best-effort: attach `spend` = honest per-field, per-turn-model dollar
    cost summed across the task's linked sessions, split MAIN vs BACKGROUND
    (claude_transcripts.live_spend_for_session — never folded together, and
    each usage field priced at its OWN rate, not a flat blend). Mirrors the
    src/override_dir resolution _attach_turn_tokens uses so both surfaces
    agree on which transcripts belong to this task. Never raises — a
    missing transcript / folder-mode-off project leaves spend at the honest
    zero shape (UI renders "no measured spend", never a fabricated figure)."""
    from prism_service.services.claude_transcripts import (
        _project_source_path,
        _merge_spend_sections,
        live_spend_for_session,
    )
    try:
        src = _project_source_path(project) or ""
    except Exception:
        src = ""
    override = ""
    try:
        from prism_service.services import claude_memory
        override = claude_memory.configured_project_dir(project) or ""
    except Exception:
        pass

    sections = []
    for s in sessions or []:
        sid = s.get("session_id") if isinstance(s, dict) else getattr(s, "session_id", "")
        if not sid:
            continue
        try:
            if override:
                sections.append(live_spend_for_session(sid, "", override_dir=override))
            elif src:
                sections.append(live_spend_for_session(sid, src))
        except Exception:
            continue

    if not sections:
        out["spend"] = live_spend_for_session("", "")
        return
    main = _merge_spend_sections([sec["main"] for sec in sections])
    background = _merge_spend_sections([sec["background"] for sec in sections])
    total = _merge_spend_sections([main, background])
    out["spend"] = {
        "main": main, "background": background, "total": total,
        "priced": total["priced"],
    }


def _git(repo: str, *args: str) -> tuple[int, str]:
    """Run one git command in `repo`, no shell, short timeout. (rc, stdout)."""
    import subprocess
    try:
        p = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=10,
        )
        return p.returncode, (p.stdout or "").strip()
    except Exception:
        return 1, ""


def _resolve_delivery_branch(repo: str, task_id: str) -> tuple[str, str]:
    """(branch, revision-to-search) for this task's [task:...] commits.

    PREREQUISITE FIX (task cb1dc6f4): this used to be the project source
    checkout's CURRENT branch (`git rev-parse --abbrev-ref HEAD`) — whatever
    the shared checkout happened to have checked out, never the task's OWN
    branch. A task's real, tagged commits live on its own
    `prism/ws/<task_id>` worktree branch (task_workspace.ensure_workspace),
    so whenever the checkout sat on unrelated work the search ran against
    the wrong ref and reported "no commits found" for tasks that plainly had
    them (live repro 2026-07-29: task a4c1bf03 reported
    branch="fix/decision-packet-visual-evidence" with 0 commits while
    prism/ws/a4c1bf03... held 2 tagged commits).

    Prefer the task's workspace branch when task_workspace still has a
    record for it AND the branch ref still resolves (a torn-down workspace
    deletes its branch too); fall back to the checkout's current branch
    otherwise — the old behavior, which still matters for tasks that never
    got a per-task worktree or whose workspace was already cleaned up after
    landing on main."""
    branch = ""
    try:
        from prism_service.services import task_workspace
        ws = task_workspace.workspace_for(task_id)
        cand = str((ws or {}).get("branch") or "")
        if cand and _git(repo, "rev-parse", "--verify", "-q", cand)[0] == 0:
            branch = cand
    except Exception:
        branch = ""
    if branch:
        return branch, branch
    _, cur = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return cur, "HEAD"


@router.get("/{task_id}/delivery")
def get_task_delivery(task_id: str, project: str = Query("default")) -> dict:
    """WHERE the task's work lives and what's left to ship (owner 2026-07-16:
    done means SHIPPED — merged and validated on main; the app must visually
    show where you are and what still needs to be done).

    Finds the task's commits by the [task:<id-prefix>] trailer convention in
    the project's git repo, then reports an honest stage pipeline:
    verified (green_gate passed) → committed → pushed (on any remote ref) →
    merged to main (ancestor of origin/main) → released (reachable from a
    tag). Every stage is computed from git facts, never inferred from the
    gate; no repo / no tagged commits yields honest empty stages."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise HTTPException(400, "bad task id")
    t = _svc(project).get(task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    verified = (getattr(t, "gate_state", "") == "passed"
                and getattr(t, "workflow_step", "") == "green_gate") \
        or getattr(t, "status", "") == "done"

    repo = ""
    try:
        from prism_service.services.claude_transcripts import _project_source_path
        repo = _project_source_path(project) or ""
    except Exception:
        pass

    commits: list[dict] = []
    branch, rev = "", "HEAD"
    if repo:
        branch, rev = _resolve_delivery_branch(repo, task_id)
        rc, log = _git(repo, "log", "--grep", f"task:{task_id[:8]}",
                       "--format=%h%x09%s", "-n", "20", rev)
        if rc == 0 and log:
            main_ref = "origin/main"
            if _git(repo, "rev-parse", "--verify", "-q", main_ref)[0] != 0:
                main_ref = "main"
            for line in log.splitlines():
                sha, _, subject = line.partition("\t")
                sha = sha.strip()
                if not sha:
                    continue
                pushed = bool(_git(repo, "branch", "-r", "--contains", sha)[1])
                merged = _git(repo, "merge-base", "--is-ancestor", sha, main_ref)[0] == 0
                tags = _git(repo, "tag", "--contains", sha)[1]
                commits.append({
                    "sha": sha, "subject": subject.strip(),
                    "pushed": pushed, "merged_to_main": merged,
                    "released_in": tags.splitlines()[0] if tags else "",
                })

    def _all(flag: str) -> bool:
        return bool(commits) and all(c[flag] for c in commits)

    # Additive OR-fallback (task 499ba9c9): SHA-ancestry (_all("merged_to_main"))
    # stays the fast path for a true merge commit (FR-2, untouched). A squash
    # or rebase merge preserves no ancestor SHA, so also check for the
    # [task:<id8>] trailer directly on origin/main via _is_shipped_on_main
    # (tasks.py:278), which matches the delimited trailer only -- never a
    # bare/prefix-collision substring (FR-5).
    merged_ok = _all("merged_to_main") or (bool(repo) and _is_shipped_on_main(repo, task_id))

    stage_states = [
        ("verified", "verified", verified,
         "green gate passed on live evidence" if verified else "gate not passed yet"),
        ("committed", "committed", bool(commits),
         f"{len(commits)} commit(s) on {branch or 'local'}" if commits
         else "no [task:…]-tagged commits found"),
        ("pushed", "pushed", _all("pushed"),
         "on a remote ref" if _all("pushed") else "branch not pushed"),
        ("merged", "merged to main", merged_ok,
         "reachable from main" if merged_ok else "no PR merged yet"),
        ("released", "released", _all("released_in"),
         (commits and commits[0].get("released_in")) or "no release tag contains this work"),
    ]
    stages, next_seen = [], False
    for key, label, ok, detail in stage_states:
        state = "done" if ok else ("next" if not next_seen else "pending")
        if not ok:
            next_seen = True
        stages.append({"key": key, "label": label, "state": state,
                       "detail": str(detail)})
    return {"branch": branch, "commits": commits, "stages": stages,
            "delivered": all(s["state"] == "done" for s in stages)}


@router.get("/{task_id}/prototype")
def get_task_prototype(
    task_id: str, project: str = Query("default"),
):
    """Serve the task's prototype HTML so the SPA can iframe it on the Plan
    card (prototypes viewable IN PRISM, not an external port). task_id is a
    server-generated UUID; reject anything else so a crafted id can't traverse
    out of the prototypes dir."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise HTTPException(400, "bad task id")
    if _svc(project).get(task_id) is None:
        raise HTTPException(404, "task not found")
    path = prototype_file(task_id)
    if not path.exists():
        raise HTTPException(404, "no prototype for this task")
    # NEVER cache a prototype. A design is ITERATED — the reviewer's feedback
    # is applied and the same URL then serves different content. FileResponse
    # sends only ETag/Last-Modified, so the browser was free to keep showing
    # the previous draft inside the iframe: the owner asked for changes, the
    # changes landed on disk, and they still saw the old design (owner
    # 2026-07-22). Stale here is not a stale asset, it is a stale DECISION.
    return FileResponse(
        str(path), media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate, no-cache"},
    )


# Evidence media a drive cites in its proof, or that the trusted-runner
# receipt captured (screenshot + walkthrough video) — whitelisted image/video
# types only, so this route can never serve executable content.
_EVIDENCE_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    # TEXT EVIDENCE (task 939756eb). A test log, a diff, or a receipt IS
    # evidence — before this the store took images/video only, so the only way
    # to show a passing run was to render a terminal to a PNG (unreadable and
    # unsearchable). Types are listed EXPLICITLY, never a catch-all, and every
    # one is inert: `text/html` is deliberately absent because an
    # attacker-supplied .html served same-origin from the evidence store would
    # be stored XSS. Anything unmapped (.html, .exe, …) still 400s.
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".diff": "text/plain; charset=utf-8",
    ".patch": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
}


def _evidence_kind(media: str) -> str:
    """Which renderer the SPA should use for an artifact."""
    if media.startswith("video/"):
        return "video"
    if media.startswith("image/"):
        return "image"
    return "text"


@router.get("/{task_id}/evidence")
def list_task_evidence(task_id: str, project: str = Query("default")) -> dict:
    """Enumerate a task's evidence store so the SPA can SHOW every captured
    artifact — not only the ones a proof string happens to cite by name. Until
    this existed the store was write-only from the UI's side: files landed in
    ``data_dir/evidence/<task_id>/`` but nothing listed them, so an approver
    could not see evidence that wasn't hand-linked in completion_proof. Returns
    each servable image/video with its /evidence/<name> url, byte size and mtime
    (newest first). ``cited`` marks files the task's completion_proof references,
    so the UI can tie a row to the specific piece it produced."""
    from datetime import datetime, timezone
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise HTTPException(400, "bad task id")
    task = _svc(project).get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    d = evidence_dir(task_id)  # mkdirs; empty dir → empty list, not a 404
    # Which filenames the proof cites, so the caller can link row → artifact.
    cited: set[str] = set()
    try:
        proof = (getattr(task, "completion_proof", "") or "") if task else ""
        for m in re.finditer(r"/evidence/([A-Za-z0-9][A-Za-z0-9._@-]{0,127})", proof):
            cited.add(m.group(1))
    except Exception:
        pass
    files = []
    for p in sorted(d.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        ext = p.suffix.lower()
        if not p.is_file() or ext not in _EVIDENCE_MEDIA:
            continue
        st = p.stat()
        files.append({
            "name": p.name,
            "url": (
                f"/api/tasks/{task_id}/evidence/{p.name}"
                f"?project={quote(project, safe='')}"
            ),
            "media_type": _EVIDENCE_MEDIA[ext],
            "kind": _evidence_kind(_EVIDENCE_MEDIA[ext]),
            "size_bytes": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            "cited": p.name in cited,
        })
    return {"files": files}


@router.get("/{task_id}/evidence/{filename}")
def get_task_evidence(
    task_id: str, filename: str, project: str = Query("default"),
):
    """Serve one of the task's evidence files (gate/audit screenshots) so the
    SPA renders it inline where the proof cites it — evidence viewable IN
    PRISM, never an external host (owner 2026-07-16). Both path pieces are
    whitelisted so a crafted request can't traverse out of the evidence dir."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise HTTPException(400, "bad task id")
    # A completion_proof image renders as a RAW <img src="/api/tasks/.../
    # evidence/x.png"> that bypasses the SPA api-client, so it carries NO
    # ?project= param and defaults to 'default' -> a 404 in any non-default
    # project, showing BROKEN visual evidence (owner 2026-07-20). The evidence
    # dir is keyed by the globally-unique task_id, not the project, so in
    # LOCAL (single-user) mode resolve the task across projects.
    # SECURITY (task b0dad59b): in TEAM mode this cross-project resolution is a
    # cross-workspace artifact bypass — a project-a member could fetch a
    # project-b task's evidence — so it is gated to local mode ONLY; team mode
    # keeps the strict per-project 404 the workspace boundary depends on.
    if _svc(project).get(task_id) is None:
        resolved = False
        try:
            from prism_service.api.auth import _auth
            _local_mode = _auth().mode != "team"
        except Exception:
            _local_mode = False
        if _local_mode:
            try:
                from prism_service import config as _cfg
                for _proj in _cfg.list_projects():
                    if _proj != project and _svc(_proj).get(task_id) is not None:
                        resolved = True
                        break
            except Exception:
                resolved = False
        if not resolved:
            raise HTTPException(404, "task not found")
    # '@' is admitted (task 47bc3982) because the browser-oracle runner names
    # every recording page@<hash>.webm — without it, every recording the
    # runner ever captures 400s here before the disk is even touched. '@' is
    # not a path separator and does not weaken the traversal guard below.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}", filename) or ".." in filename:
        raise HTTPException(400, "bad filename")
    ext = filename[filename.rfind(".") :].lower() if "." in filename else ""
    media = _EVIDENCE_MEDIA.get(ext)
    if not media:
        raise HTTPException(400, "unsupported evidence type")
    path = evidence_dir(task_id) / filename
    if not path.exists():
        raise HTTPException(404, "no such evidence file")
    # no-cache (NOT no-store): evidence files get retaken in place under the
    # same name, so the browser must revalidate; the etag still yields a 304
    # when the file is unchanged.
    return FileResponse(
        str(path), media_type=media, headers={"Cache-Control": "no-cache"}
    )


def _clean_doc(doc: str) -> str:
    """Collapse whitespace and demote RST double-backtick literals so a test
    docstring reads as plain prose in the UI panel."""
    doc = (doc or "").strip()
    doc = re.sub(r"``([^`]+)``", r"\1", doc)
    return " ".join(doc.split())


_TASK_ID_RE = re.compile(
    r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?\b")


def file_owns_task(source: str, task_id: str) -> bool:
    """True when this test module BELONGS to ``task_id`` — as opposed to
    merely mentioning it (task e0149f1f, 2026-07-21).

    The old predicate was ``task_id in text``: a bare substring scan over the
    whole file. Any test that CITED another task in prose ("see task <id>",
    "Regression: task <id>") was therefore attributed to the cited task, and
    the gate panel for 5a6837a0 listed 33 tests over three files of which only
    12 were its own — the owner was shown another ticket's tests as if they
    were this ticket's.

    Ownership rule: the FIRST task id named in the module docstring is the
    owner (the convention this suite already uses, e.g. "Pin the gate tooth
    (task <id>)." / "Pins task <id>.") — a later citation never transfers
    ownership. But a POSSESSIVE mention ("task <id>'s gate/telemetry/receipt")
    is always talking ABOUT that other task's thing, never declaring this
    file's own ownership, so it is skipped when hunting for the first real
    owner (task 9f3c57dc, defect 3): "Owner 2026-07-29, reviewing task
    ae67ed5c's gate: ..." was exactly this shape and silently inflated
    ae67ed5c's pinned count from 6 to 8 — commit ce1c3c4 hand-patched that one
    file's wording; this fixes the mechanism so no future file needs the same
    hand patch. If EVERY mention in the docstring is possessive, there is no
    declared owner at all. Files we cannot parse, or that carry no module
    docstring, fall back to the old mention behaviour so a task never
    silently LOSES its pins.
    """
    text = source or ""
    tid = (task_id or "").strip()
    if not tid:
        return False
    short = tid[:8]
    mentioned = tid in text or (len(short) >= 8 and short in text)
    if not mentioned:
        return False
    try:
        import ast
        doc = ast.get_docstring(ast.parse(text))
    except Exception:
        return mentioned          # unparseable — keep the old behaviour
    if not doc:
        return mentioned          # no docstring — keep the old behaviour
    owner = None
    for m in _TASK_ID_RE.finditer(doc):
        tail = doc[m.end():m.end() + 2]
        if tail.startswith("'s") or tail.startswith("’s"):
            continue  # possessive — cites that task's THING, not ownership
        owner = m.group(0)
        break
    if owner is None:
        # every mention was possessive (or none existed) — no declared owner,
        # a bare/possessive citation never attributes the file (defect 3).
        return False
    return owner[:8].lower() == short.lower()


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


def _checkout_tests_root():
    """The service checkout's own tests/ dir. Split out from get_task_tests
    so it can be pointed at a synthetic root in tests without touching the
    real repo (task 9f3c57dc)."""
    from pathlib import Path
    return Path(__file__).resolve().parents[2] / "tests"


@router.get("/{task_id}/tests")
def get_task_tests(task_id: str, run: bool = Query(False),
                   project: str = Query("default")):
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
    if _svc(project).get(task_id) is None:
        raise HTTPException(404, "task not found")

    from pathlib import Path
    from prism_service.services.task_workspace import workspace_for

    short = task_id[:8]
    results: list[dict] = []
    files: list[str] = []
    seen_rel: set[str] = set()
    # Grouped by the ROOT each file was discovered under (task 9f3c57dc,
    # defect 2) — a file found via the workspace scan must be RUN against the
    # workspace, a file found via the checkout scan must be run against the
    # checkout, never pooled into one invocation against a single fixed root.
    files_by_root: dict[Path, list[str]] = {}

    # Scan TWO roots: the service checkout's tests/ (tests already merged to
    # main) AND the task's own git worktree tests/ (work that still lives only
    # in the task workspace — the common case for a just-finished, unmerged
    # task like this one). Without the workspace root the gate's test evidence
    # is invisible for every unmerged task, which reads as "no evidence".
    checkout_root = _checkout_tests_root()
    ws = workspace_for(task_id)
    _ws_tests = (Path(ws["path"]) / "services" / "prism-service" / "tests"
                 if ws else None)
    # SAME-TREE RULE (owner 2026-07-21: "fake facts in prism"). The row LIST
    # and the live RUN must come from ONE tree. We execute in the task's
    # workspace whenever it exists, so the workspace must also be scanned
    # FIRST — it wins the rel-path dedup below. Listing from the merged
    # checkout (14 tests at branch HEAD) while running in the workspace (13
    # tests at its older HEAD) produced a phantom amber "13 / 14 passing":
    # a denominator from one commit over a numerator from another, with the
    # test that exists only in the newer tree silently counted "not-run".
    _use_ws = bool(_ws_tests and _ws_tests.is_dir())
    roots: list[Path] = ([_ws_tests, checkout_root] if _use_ws
                         else [checkout_root])

    for tests_root in roots:
        if not tests_root.is_dir():
            continue
        tests_root_run_root = tests_root.parent
        for p in sorted(tests_root.rglob("*.py")):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            # OWNERSHIP, not mention (task e0149f1f) — see file_owns_task.
            if file_owns_task(text, task_id):
                rel = p.relative_to(tests_root.parent).as_posix()
                if rel in seen_rel:
                    continue
                seen_rel.add(rel)
                files.append(rel)
                files_by_root.setdefault(tests_root_run_root, []).append(rel)
                results.extend(_extract_tests_from_source(text, rel))
    ran = False
    could_not_run = False
    if run and files and results:
        # ONE _run_pinned_tests call PER ROOT (task 9f3c57dc, defect 2) — a
        # file that exists only in the checkout must never be pooled with a
        # file that exists only in the workspace into a single invocation,
        # because pytest aborts collection for the WHOLE argv the instant one
        # path is missing from cwd. Running each group against the root it
        # was actually discovered in means a group that cannot run never
        # drags down a group that can, and no file is ever run from a tree
        # OTHER than the one it was pinned from (the tree under review).
        merged_statuses: dict[str, str] = {}
        any_group_ran = False
        for group_root, group_files in files_by_root.items():
            statuses = _run_pinned_tests(group_root, group_files)
            if statuses is not None:
                any_group_ran = True
                merged_statuses.update(statuses)
        if any_group_ran:
            ran = True
            for row in results:
                row["status"] = merged_statuses.get(row["name"], "not-run")
        else:
            # every group failed to collect/execute — a genuine COULD NOT
            # RUN, never a numeric 0 / N with a fresh timestamp (defect 1).
            could_not_run = True
    # HONEST OBSERVATION (trust fix, task 45e04fad follow-up): when we did NOT
    # re-run live, reflect the gate's own trusted-runner EvidenceReceipt so a
    # done/green-gated task shows the TRUE result the gate already observed —
    # never a false "not-run" on tests the trusted runner proved pass. The
    # receipt is the source of truth and is cited so the claim is checkable.
    receipt_info = None
    try:
        from prism_service.services import oracle_spec as _osp
        rc = _osp.latest_receipt(project, task_id)
    except Exception:
        rc = None
    if rc is not None:
        # Always surface the gate's receipt as provenance (even on a live
        # re-run) so the tab can cite what the trusted runner observed.
        # FRESHNESS — owner 2026-07-21: "if you are giving me fake facts in
        # prism ... then i have no way forward". A receipt is bound to the tree
        # it ran against; when that is NOT the tree under review its numbers are
        # HISTORY, not the current verdict. Returning tree_sha alone was not
        # enough: the gate card rendered a stale "13 / 14 passing" (measured at
        # b04be24, before the follow-up fix landed) in amber under the label
        # "reflects the gate's trusted-runner result", so a reader could not
        # tell a past observation from the present one. Say it explicitly.
        _cur_tree = ""
        try:
            _cur_tree = _osp.current_tree_sha(ws["path"] if ws else None)
        except Exception:
            _cur_tree = ""
        receipt_info = {
            "job_id": rc.job_id, "tree_sha": rc.tree_sha,
            "passed": bool(rc.passed), "status": rc.status,
            "ended_at": rc.ended_at, "runner": rc.runner_version,
            "reason": (rc.reason or "")[:400],
            "current_tree_sha": _cur_tree,
            "stale": bool(_cur_tree and rc.tree_sha
                          and rc.tree_sha != _cur_tree),
        }
        # Fill any row the live run did NOT resolve (or when we didn't run
        # live at all) from the gate's trusted-runner receipt. A real live run
        # keeps its own passed/failed statuses (they win); but a live run that
        # executed in a stale/wrong tree — e.g. a just-MERGED task whose scratch
        # worktree still holds pre-merge code — yields no usable status and
        # leaves rows "not-run", which must NOT hide the distinct-actor receipt
        # (owner 2026-07-20: a done, green-gated task's pinned tests read green).
        # The receipt runs the task's PINNED oracle files; its reason names each
        # path, so rc.passed means every pinned test passed under the runner.
        reason = rc.reason or ""
        for row in results:
            if row.get("status") not in (None, "", "not-run"):
                continue
            f = row.get("file") or ""
            if f and f in reason:
                row["status"] = "passed" if rc.passed else "failed"
                row["passed"] = bool(rc.passed)
                row["verified_by"] = "gate-receipt"
    # Name the tree the rows were listed from AND run in, so the count on the
    # card is checkable instead of merely trusted (same owner directive).
    _src_sha = ""
    try:
        from prism_service.services import oracle_spec as _osp3
        _src_sha = _osp3.current_tree_sha(
            str(_ws_tests.parent.parent.parent) if _use_ws
            else str(checkout_root.parent.parent.parent))
    except Exception:
        _src_sha = ""
    # PERSIST + HYDRATE (task f3e8d477). A run used to be computed and thrown
    # away with the response, so the tab printed NOT RUN forever and the
    # evidence package could never answer "did the tests run?". Now a real run
    # is recorded against the tree it ran on, and EVERY read (including the
    # cheap run=false page load, which still never spawns pytest) stamps each
    # row from that record. A record from another tree is marked stale; a test
    # with no record stays not-run.
    try:
        from prism_service.services import test_run_store as _trs
        _store = _trs.get_test_run_store()
        if ran:
            _store.record_run(task_id, _src_sha, results)
        results = _trs.hydrate(_store, task_id, results, _src_sha)
    except Exception:
        pass  # best-effort: never fail the tab over its own evidence store

    return {"tests": results, "ran": ran,
            # COULD NOT RUN (task 9f3c57dc, defect 1): distinct from ran=False
            # "never attempted" — this run WAS attempted, collected nothing,
            # and must never be read as a numeric 0 / N with a fresh
            # timestamp. Sibling ticket 72d3e0d1 renders this on the card.
            "could_not_run": could_not_run,
            "receipt": receipt_info,
            "source": ("workspace" if _use_ws else "checkout"),
            "source_sha": _src_sha}


def _run_pinned_tests(service_root, files: list[str]) -> Optional[dict]:
    """Run the pinned test files (bounded) and map test name -> outcome via
    pytest's line report. Returns None when the run itself could not happen
    (missing interpreter, timeout, or — task 9f3c57dc, defect 1 — pytest ran
    but COLLECTED NOTHING, e.g. "file or directory not found ... collected 0
    items"). The caller only ever calls this with files that own at least one
    discovered ``def test_*``, so a genuine run always emits at least one
    PASSED/FAILED/ERROR/SKIPPED line; an EMPTY statuses dict therefore means
    the run itself did not happen, never that 0 tests passed. Returning {} in
    that case used to be read by the caller as a confident, arithmetically
    false 0 / N — callers must omit statuses honestly instead."""
    if not files:
        return None
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
        if not statuses:
            return None  # collected nothing — could not run, not "0 passed"
        return statuses
    except Exception:
        return None


def _discover_pinned_test_rows(task_id: str) -> list[dict]:
    """Fallback assertion-source discovery for a proof_type=test gate whose
    receipt predates (or whose adapter skips) the mint-time
    ``assertion_source`` capture: scan the same two roots ``/tests`` uses
    (service checkout + task workspace) for files that pin this task by id,
    and hand every ``def test_*`` to ``_extract_tests_from_source`` for its
    VERBATIM body — never a paraphrase. Best-effort; a missing workspace or
    unparseable file yields fewer rows, never an error."""
    from pathlib import Path
    from prism_service.services.task_workspace import workspace_for

    short = task_id[:8]
    results: list[dict] = []
    seen_rel: set[str] = set()
    checkout_root = Path(__file__).resolve().parents[2] / "tests"
    roots: list[Path] = [checkout_root]
    ws = workspace_for(task_id)
    if ws:
        roots.append(Path(ws["path"]) / "services" / "prism-service" / "tests")
    for tests_root in roots:
        if not tests_root.is_dir():
            continue
        for p in sorted(tests_root.rglob("*.py")):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            # OWNERSHIP, not mention (task e0149f1f) — see file_owns_task.
            if file_owns_task(text, task_id):
                rel = p.relative_to(tests_root.parent).as_posix()
                if rel in seen_rel:
                    continue
                seen_rel.add(rel)
                results.extend(_extract_tests_from_source(text, rel))
    return results


@router.get("/{task_id}/gate_evidence")
def get_task_gate_evidence(task_id: str, project: str = Query("default")) -> dict:
    """Project the task's newest EvidenceReceipt into what a gate CARD needs
    to show: captured screenshot(s)/video (re-pointed at the whitelisted
    ``/evidence/<file>`` route), verbatim per-test assertion source, and the
    capture provenance (who/when/build/tree).

    CRITICAL (likely_misfire): this renders ONLY the RECEIPT's own
    ``artifacts[]`` — what the trusted runner actually captured — never the
    driving agent's hand-attached completion_proof markdown. A gate with none
    of the three modalities (screenshot, video, assertion source) must come
    back with all three lists empty, so the caller can render an honest
    "no captured evidence — not evidence-backed" state instead of looking
    evidence-backed on borrowed prose.
    """
    from pathlib import Path

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise HTTPException(400, "bad task id")
    task = _svc(project).get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")

    from prism_service.services import oracle_spec as _osp
    try:
        receipt = _osp.latest_receipt(project, task_id)
    except Exception:
        receipt = None

    artifacts: list[dict] = []
    assertions: list[dict] = []
    captured_by = captured_at = build = ""
    tree_sha = (receipt.tree_sha if receipt else "") or ""

    if receipt is not None:
        for art in receipt.artifacts or []:
            if not isinstance(art, dict):
                continue
            kind = art.get("kind", "")
            prov = art.get("provenance") or {}
            if kind in ("screenshot", "video"):
                raw_path = str(art.get("path") or "")
                name = Path(raw_path).name if raw_path else ""
                ext = name[name.rfind(".") :].lower() if "." in name else ""
                # Never trust the receipt's raw path verbatim — only serve a
                # name that resolves under this task's evidence dir with a
                # whitelisted extension (same guard as get_task_evidence).
                if name and ext in _EVIDENCE_MEDIA and (evidence_dir(task_id) / name).is_file():
                    artifacts.append({
                        "kind": kind,
                        "path": (
                            f"/api/tasks/{task_id}/evidence/{name}"
                            f"?project={quote(project, safe='')}"
                        ),
                        "provenance": prov,
                    })
                    captured_by = captured_by or str(prov.get("captured_by", ""))
                    captured_at = captured_at or str(prov.get("captured_at", ""))
                    build = build or str(prov.get("build", ""))
            elif kind == "assertion_source":
                src = str(prov.get("source", "") or "")
                if src:
                    assertions.append({"id": art.get("path", ""), "source": src})

    # Fallback for proof_type=test gates whose receipt predates (or whose
    # adapter skips) mint-time assertion_source capture — reuse the SAME
    # discovery + VERBATIM extraction the Tests tab uses (task 25a25d84: do
    # not re-paraphrase what a test asserts).
    if not assertions and str(getattr(task, "proof_type", "") or "").strip().lower() == "test":
        for row in _discover_pinned_test_rows(task_id):
            snippet = row.get("snippet") or ""
            if snippet:
                assertions.append({"id": row.get("name", ""), "source": snippet})

    return {
        "artifacts": artifacts,
        "captured_by": captured_by,
        "captured_at": captured_at,
        "build": build,
        "tree_sha": tree_sha,
        "assertions": assertions,
    }


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    # A task's DESCRIPTION was settable at create and then frozen forever —
    # neither this route nor mcp task_update could touch it. When a design is
    # corrected mid-flight (owner reversed the whole access model on 6cef97ec,
    # 2026-07-22) the description keeps asserting the retired premise, which is
    # the first thing any reader sees. TaskService.update already accepts
    # arbitrary fields; only this model was blocking it.
    description: Optional[str] = None
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
