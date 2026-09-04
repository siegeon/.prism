"""Conductor API — prompt variants, scores, session outcomes, and SDLC state."""

import threading
import time
import weakref

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from prism_service.project_context import get_project
from prism_service.services.conductor_service import board_health
from prism_service.services import drive_heartbeat, task_workspace

router = APIRouter()


def _svc(project: str):
    try:
        return get_project(project).conductor_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")


# /state recompute is GIL-bound Python over the whole task store (per managed
# task: history scans, per-step medians, live transcript token reads) — ~0.5s
# idle and it SERIALIZES under load: 6 concurrent pollers each measured 7.5s
# (2026-08-13) while LiveBar (every page) + ConductorPage + SSE-triggered
# refetches all hit it. A short TTL cache with single-flight collapses any
# number of pollers into one recompute per TTL. Keyed on the SERVICE INSTANCE
# via WeakKeyDictionary so each test's fresh service gets a fresh cache and
# nothing leaks across tests; 2.5s stays under every poller's own cadence.
_STATE_TTL_S = 2.5
_state_cache: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_state_lock = threading.Lock()


def _state_cache_get(s):
    """None when uncached — or when the service isn't weakref-able (test
    stubs), which simply disables caching for it."""
    try:
        return _state_cache.get(s)
    except TypeError:
        return None


def _state_cache_put(s, val) -> None:
    try:
        _state_cache[s] = val
    except TypeError:
        pass


def _scores_db_of(s) -> str:
    """The scores.db path backing THIS conductor service instance —
    conductor_service.ConductorService already stores it (self._scores_db,
    conductor_service.py:637), so this reads the live instance rather than
    re-deriving a path from get_project() (which test doubles need not
    implement _data_dir for)."""
    return getattr(s, "_scores_db", "")


# Task e9625a4d: a NEW threshold derived FROM the settled 180s
# HEARTBEAT_WINDOW_S (mx-b26121) -- longer than one ordinary driving window,
# so a report that merely just fell outside HEARTBEAT_WINDOW_S (the normal
# middle of a step) never reads as lost; only long-term silence does.
SIGNAL_LOST_AFTER_S = drive_heartbeat.HEARTBEAT_WINDOW_S * 3


_REPORT_SIGNAL_STATES = frozenset({"adrift", "stalled"})


def _with_report_signal(managed_tasks: list, scores_db: str) -> list:
    """Task e9625a4d, extended by task 0f090a6c: distinguish a freshly
    adrift/stalled row from one whose task-scoped report signal
    (drive_heartbeat) has gone dark for a long while -- additive enrichment
    mirroring _with_claimed (:53-66) below. NEVER edits
    conductor_service.activity_for (a control_plane.POLICY_FILES entry) and
    never widens the settled 120s/90s/180s thresholds (mx-b26121); this only
    READS drive_heartbeat.heartbeat_age_s, the same non-policy primitive
    activity_for already consults.

    Task 0f090a6c SUPERSESSION: originally scoped to 'adrift' rows only
    ("working/driving/stalled/paused already carry their own honest
    wording") -- that was wrong for 'stalled', which is exactly the OTHER
    claimed-but-unobservable classification and needs the identical
    treatment (see test_invisible_worker_states_read_cannot_see.py). Also
    attaches report_signal_age_s, the REAL heartbeat age, so the client
    never has to fall back to task_motion_s (step-transition recency, a
    different clock) for the elapsed-since-observation number."""
    out = []
    for row in managed_tasks:
        row = dict(row)
        activity = row.get("activity") or {}
        if activity.get("state") in _REPORT_SIGNAL_STATES:
            activity = dict(activity)
            age = drive_heartbeat.heartbeat_age_s(scores_db, row.get("id", ""))
            activity["report_signal_lost"] = age is None or age > SIGNAL_LOST_AFTER_S
            activity["report_signal_age_s"] = age
            row["activity"] = activity
        out.append(row)
    return out


def _with_claimed(managed_tasks: list) -> list:
    """Task 2dfa94bd: a task only ever renders SDLC-drive chrome on the real
    /conductor tab if conductor_work actually claimed it (ensure_workspace
    ran at its first PEEK). task_workspace.workspace_record is a real,
    side-effect-free "has conductor_work ever touched this task" bit, None
    until a genuine claim and a real dict thereafter (no recency check, so a
    released-but-idle worker still reads claimed:True). Additive field —
    module-attribute lookup so tests can monkeypatch task_workspace.workspace_record."""
    out = []
    for row in managed_tasks:
        row = dict(row)
        row["claimed"] = task_workspace.workspace_record(row.get("id", "")) is not None
        out.append(row)
    return out


def _state_payload(s) -> dict:
    hit = _state_cache_get(s)
    if hit and time.monotonic() - hit[0] < _STATE_TTL_S:
        return hit[1]
    if not _state_lock.acquire(blocking=False):
        # Another request is already recomputing — serve the stale copy
        # instantly rather than queueing a second identical recompute.
        if hit:
            return hit[1]
        _state_lock.acquire()
    try:
        hit = _state_cache_get(s)
        if hit and time.monotonic() - hit[0] < _STATE_TTL_S:
            return hit[1]
        payload = {
            "exploration_rate": s.exploration_rate(),
            "variants": s.get_variants(),
            "scores": s.get_scores(),
            "retired": s.get_retired(),
            # Conductor v2 (#79 follow-up): SPA /conductor page reads these to
            # render the SDLC dashboard — which tasks conductor is driving and
            # where they are in the workflow.
            "managed_tasks": _with_claimed(s.managed_tasks()),
            "step_buckets": s.step_buckets(),
            # GoalBuddy GAP-5: cross-task "reorient" signal composed from the
            # per-task '⚠' advisory notes — fires when >= 2 low-value done-root
            # completions lead the newest-first run. Additive; SPA renders a badge.
            "board_health": board_health(_board_tasks(s)),
        }
        _state_cache_put(s, (time.monotonic(), payload))
        return payload
    finally:
        _state_lock.release()


@router.get("/state")
def state(project: str = Query("default"), outcomes_limit: int = Query(200, ge=1, le=1000),
          include_outcomes: bool = Query(False)) -> dict:
    s = _svc(project)
    # Shallow copy so the include_outcomes branch below never mutates the
    # cached payload shared with concurrent requests.
    out = dict(_state_payload(s))
    # Task e9625a4d: additive report-signal enrichment (see _with_report_signal
    # docstring) -- applied outside the TTL cache so staleness stays live.
    out["managed_tasks"] = _with_report_signal(out["managed_tasks"], _scores_db_of(s))
    # Task d5465a25 (heavy-poll scoping): session_outcomes is 93% of this
    # payload (55.9 KB of a measured 60 KB) and this route is polled every
    # 5s from LiveBar.tsx + ConductorPage.tsx — neither reads it (both type
    # only managed_tasks/step_buckets/board_health). SessionsPage and
    # SessionDetailPage get their OWN copy from /api/sessions
    # (api/sessions.py independently calls get_session_outcomes), so this
    # was dead weight on the polled path. Ship it only on explicit opt-in,
    # mirroring the /api/version?notes=true pattern (task 842248bd).
    if include_outcomes:
        out["session_outcomes"] = s.get_session_outcomes(limit=outcomes_limit)
    return out


def _board_tasks(s) -> list:
    """All tasks for the board_health scan (done-root filtering happens there)."""
    svc = getattr(s, "_task_svc", None)
    if svc is None:
        return []
    try:
        return svc.list()
    except Exception:
        return []


@router.get("/decision-packet")
def decision_packet_route(task_id: str, project: str = Query("default")) -> dict:
    """Server-assembled evidence packet for the approval panel (task a1e4120f).
    A read-only VIEW over the task's REAL worktree artifacts (git diff/log vs
    baseline, the oracle receipt, evidence screenshots) so a gate never has to
    show a bare 'No recorded evidence' box. 404 only for an unknown task; an
    artifact-less task still returns a well-formed (empty) packet, never a 500."""
    from prism_service.services import decision_packet
    s = _svc(project)
    task = getattr(s, "_task_svc", None) and s._task_svc.get(task_id)
    if task is None:
        raise HTTPException(404, "unknown task")
    return decision_packet.assemble_packet(project, task_id, task)


@router.get("/design-packet")
def design_packet_route(task_id: str, project: str = Query("default")) -> dict:
    """FR-1/FR-2/FR-3 (task c016667f): the ONE assembled, addressable design
    packet for the Plan card - plan_doc + plan_diagram + prototype bytes +
    oracle + likely_misfire + the order report, plus the live approval
    status (never a stored flag) so the card and the gate agree."""
    from prism_service.services import design_packet as dp
    s = _svc(project)
    task = getattr(s, "_task_svc", None) and s._task_svc.get(task_id)
    if task is None:
        raise HTTPException(404, "unknown task")
    packet = dp.assemble_packet(project, task_id, task)
    packet["approval"] = dp.approval_status(project, task_id, task)
    return packet


@router.post("/design-packet/approve")
def design_packet_approve(task_id: str = Body(..., embed=True),
                          approver: str = Body(..., embed=True),
                          project: str = Query("default")) -> dict:
    """FR-6/FR-7: the ONLY way a design-packet approval is recorded - an
    explicit owner write action, never a render/browser receipt (design_
    packet.record_approval raises ValueError on a non-owner_explicit method,
    which this route surfaces as 400 rather than silently accepting it).

    Task 98d38111: `approver` must resolve to a real HUMAN identity via
    ActorService (a real user id/email in the workspace store) before the
    ledger writes an owner_explicit receipt for it - refusing boilerplate
    audit-reason text or any other unresolvable string, so the receipt this
    tooth exists to demand can never be forged for nobody."""
    from prism_service.services import actor_service as actor_service_module
    from prism_service.services import design_packet as dp
    from prism_service.models.actor import ActorKind
    s = _svc(project)
    task = getattr(s, "_task_svc", None) and s._task_svc.get(task_id)
    if task is None:
        raise HTTPException(404, "unknown task")
    identity = actor_service_module.get_actor_service().resolve(approver)
    if identity.kind != ActorKind.HUMAN:
        raise HTTPException(
            400,
            f"approver {approver!r} does not resolve to a real signed-in "
            "identity - a design-packet approval must name a real user")
    try:
        appr = dp.record_approval(project, task_id, task, approver=approver,
                                  method="owner_explicit")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "approver": appr.approver,
            "packet_hash": appr.packet_hash, "approved_at": appr.approved_at}


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
    # DIRTY JUDGE (task 69233ca0): the daemon's OWN checkout — never a task
    # worktree — carrying an uncommitted edit to a control_plane.POLICY_FILES
    # entry means the running judge cannot prove it is executing the pinned
    # code. Checked FIRST, ahead of every gate-specific branch below, so a
    # driver and a human both see the named dirty file no matter what kind of
    # gate this is. NOT a refusal (follow-up correction, task 69233ca0): the
    # MACHINE seat abstains on this same signal (conductor_service.py's
    # adjudicate_* methods pre-flight control_plane.
    # candidate_controls_judge_reason and leave the gate pending) — but a
    # HUMAN who has SEEN this caveat may still click Approve; gate_decide
    # records it into gate_reason + history rather than failing. receipt_ok
    # True + manual_review True says exactly that: not machine-decidable, not
    # blocked either. A clean checkout falls straight through, untouched.
    from prism_service.services import control_plane as _cp
    _dirty_reason = _cp.dirty_judge_reason()
    if _dirty_reason:
        return {"receipt_ok": True, "manual_review": True,
                "receipt_refusal": "",
                "receipt": {"adapter": "control-plane", "passed": True,
                            "status": "judge_dirty_caveat", "ended_at": "",
                            "reason": _dirty_reason}}
    # SETTLED GATE READINESS (task 39adc067, e0149f1f mirror image): a gate
    # that is ALREADY DECIDED (task.gate_state == "passed") must never be
    # re-litigated through the same STALE-tree refusal text a genuinely
    # unsound PENDING approval would produce - a reader could not otherwise
    # tell a sound decided gate from a false-green one. Fires ONLY for a
    # settled gate; gate_state != "passed" falls through to the branches
    # below unchanged, so the e0149f1f false-green catch for PENDING gates
    # is untouched (task stop_if).
    if str(getattr(task, "gate_state", "") or "").strip().lower() == "passed":
        import re as _re_settled
        _reason_txt = str(getattr(task, "gate_reason", "") or "")
        _m = _re_settled.search(r"tree=([0-9a-f]{6,40})", _reason_txt)
        _decided_tree = _m.group(1) if _m else ""
        _drift_note = ""
        if _decided_tree:
            try:
                from prism_service.services import oracle_spec as osp
                from prism_service.services import task_workspace
                ws = task_workspace.workspace_for(task_id)
                _cur = osp.current_tree_sha(
                    (ws or {}).get("path") if ws else None)
                if _cur and not _cur.startswith(_decided_tree):
                    _drift_note = (
                        " (informational: the tree has moved to "
                        f"{_cur[:12]} since this gate was decided at "
                        f"{_decided_tree} - the decision still stands)")
            except Exception:
                pass
        _settled_reason = (f"gate already decided at tree={_decided_tree}"
                           if _decided_tree else "gate already decided")
        if _reason_txt:
            _settled_reason += f" - {_reason_txt}"
        _settled_reason += _drift_note
        return {"receipt_ok": True, "receipt_refusal": "",
                "manual_review": True,
                "receipt": {"adapter": "settled", "passed": True,
                            "status": "decided", "ended_at": "",
                            "reason": _settled_reason}}
    # PLAN-GATE READINESS (task c016667f, FR-10): the design-packet approval
    # status IS the live parked reason - self-diagnosable without a stale
    # gate_reason relay, mirroring the red_gate branch below.
    if getattr(task, "workflow_step", "") == "plan_gate":
        # Owner 2026-08-27 (task 3c774abd): the design-packet approval IS the
        # owner's plan stop on a root task (adapter design-packet, below); a
        # child task never parks here because its autoclear skips the ledger.
        from prism_service.services import design_packet as dp
        _status = dp.approval_status(project, task_id, task)
        if _status.get("approved"):
            return {"receipt_ok": True, "receipt_refusal": "",
                    "manual_review": True,
                    "receipt": {"adapter": "design-packet", "passed": True,
                                "status": "approved", "ended_at": "",
                                "reason": ("design packet approved - "
                                           "Approve to release the gate")}}
        _reason = str(_status.get("reason", "") or
                      "design packet needs an explicit owner approval")
        return {"receipt_ok": False, "receipt_refusal": _reason,
                "manual_review": True,
                "receipt": {"adapter": "design-packet", "passed": False,
                            "status": ("stale" if _status.get("stale")
                                      else "pending"),
                            "ended_at": "", "reason": _reason}}
    # RED-GATE READINESS (task a5e8d877, owner 2026-07-16): a red gate was
    # judged with the GREEN oracle tooth, so every test-proof red gate read
    # 'evidence not on file' with no possible action — a dead-end. Judge RED
    # evidence for red gates; green gates keep the receipt tooth below.
    if getattr(task, "workflow_step", "") == "red_gate":
        pt = str(getattr(task, "proof_type", "") or "").strip().lower()
        if pt == "demo":
            return {"receipt_ok": True, "receipt_refusal": "",
                    "receipt": {"adapter": "demo-rubric", "passed": True,
                                "status": "demo", "ended_at": "",
                                "reason": ("demo ticket: red is the absent "
                                           "artifact; the rubric decides "
                                           "on Approve")}}
        from prism_service.services import oracle_spec as osp
        from prism_service.services.conductor_service import _red_pytest_spec
        # MUST be the same spec the mint and the adjudicator use (task
        # a9215794): spec_hash is the receipt's freshness key, so deriving a
        # different spec here would never match the receipt just minted.
        spec = _red_pytest_spec(task)
        red_sha = s._red_step_sha(task_id)
        fresh = (osp.fresh_red_receipt(project, task_id, red_sha,
                                       spec.spec_hash())
                 if (red_sha and spec is not None) else None)
        if fresh is not None:
            return {"receipt_ok": True, "receipt_refusal": "",
                    "receipt": {"adapter": fresh.adapter, "passed": False,
                                "status": fresh.status,
                                "ended_at": fresh.ended_at,
                                "reason": str(fresh.reason)[:300]}}
        # SWEPT AND REFUSED (task 5c61e0e6): fresh_red_receipt only matches
        # status==ST_RED, so a receipt already on file at this red_sha+spec
        # with a NOT-red verdict (the seat ran run_red_oracle, the pinned
        # suite PASSED at the immutable anchor, and adjudicate_test_red_gate's
        # own `tried` guard abstains forever once ANY receipt exists there)
        # is invisible to `fresh` but is NOT unswept. Read it explicitly so
        # readiness never promises a sweep that structurally cannot come.
        _swept = None
        if red_sha and spec is not None:
            for _r in reversed(osp.read_receipts(project, task_id)):
                if _r.tree_sha == red_sha and _r.spec_hash == spec.spec_hash():
                    _swept = _r
                    break
        if _swept is not None and _swept.status != osp.ST_RED:
            return {"receipt_ok": False,
                    "receipt_refusal": (
                        f"{str(_swept.reason)[:300]} — the machine seat "
                        "already swept this anchor and will not retry (the "
                        "pinned suite passes there, so a red demonstration "
                        "can never be produced from this history). This "
                        "gate needs a distinct actor's decision now.")}
        # Does the machine red seat actually TAKE this ticket? Promising "no
        # owner action needed" for a sweep that can never come is how an owner
        # waits forever (owner 2026-07-21: task 89e90d1a sat at red_gate while
        # the card said the machine had it). Name the real blocker instead.
        _blocks: list[str] = []
        try:
            from prism_service.services import gate_adjudicator as _ga
            if not _ga.is_enabled():
                _blocks.append("the machine gate seat is off in this "
                               "environment (PRISM_GATE_ADJUDICATOR_INTERVAL)")
        except Exception:
            pass
        if spec is None:
            _blocks.append("this ticket has no pinned pytest material to "
                           "demonstrate red with (set task.verify to the "
                           "failing test path(s))")
        if not red_sha:
            return {"receipt_ok": False,
                    "receipt_refusal": (
                        "no red-step commit derivable — commit the failing "
                        "tests with a [task:<id>] trailer, or attest a "
                        "pre-change ref by adding a `red-anchor-ref: <sha>` "
                        "marker line to the red-step completion_proof, or "
                        "decide the gate manually")}
        if _blocks:
            return {"receipt_ok": False,
                    "receipt_refusal": (
                        f"no RED receipt, and NO machine sweep is coming: "
                        f"{'; '.join(_blocks)}. The red anchor "
                        f"{red_sha[:12]} is on file — re-running the oracle "
                        "cannot help (it mints a PASSING receipt; a red gate "
                        "needs the pinned tests observed FAILING there). "
                        "This gate needs a distinct actor's decision.")}
        return {"receipt_ok": False,
                "receipt_refusal": (
                    "no RED receipt yet — the machine seat will "
                    "demonstrate the pinned tests failing at red-step "
                    f"commit {red_sha[:12]} on its next sweep and decide "
                    "this gate; no owner action needed")}
    # STORY-GATE READINESS (task a646cbd1): a story_gate task with a rubric
    # refusal fell through to the green-gate-shaped EvidenceReceipt oracle
    # tooth below — wrong evaluation entirely for a rubric gate. Judge with
    # the SAME rubric evaluator the gate's own approve path uses
    # (ConductorService._verify_rubric_gate -> arc_governance), read-only —
    # mirrors the plan_gate/red_gate branches above so the reason a driver
    # or human sees here is the LIVE story_complete verdict, not a stale or
    # mismatched-gate string.
    if getattr(task, "workflow_step", "") == "story_gate":
        live = s._verify_rubric_gate(task, "story_complete")
        _reason = str(live.get("reason", "") or "")
        _ok = bool(live.get("verified"))
        return {"receipt_ok": _ok, "receipt_refusal": "" if _ok else _reason,
                "receipt": {"adapter": "story-rubric", "passed": _ok,
                            "status": "story_complete" if _ok else "pending",
                            "ended_at": "", "reason": _reason}}
    # EPIC ROLL-UP (issue #171, owner 2026-07-19): a parent whose children all
    # rolled up cleanly is signable — the children's proofs ARE the epic's proof,
    # and gate_decide's rollup path accepts a plain distinct-actor approve (no
    # verifier, no artifact). Present READY so the owner's single roll-up sign-off
    # is ENABLED; else surface the actionable roll-up reason (which child blocks).
    try:
        from prism_service.services.conductor_service import (
            epic_rollup_verdict, ui_artifact_gate_reason, has_captured_evidence,
            _task_attr, subtree_progress_counts)
        # parent_id-scoped (idx_tasks_parent) rather than a full-table read
        # filtered in Python — same rows, one indexed query.
        kids = list(s._task_svc.list(parent_id=task_id))
        # task 16388421: TaskService.list returns cancelled AND soft-deleted
        # rows too, so a task whose ONLY child is dead still had a non-empty
        # `kids` and entered this branch — even though epic_rollup_verdict
        # itself would say "not an epic" given the chance. Filter to LIVE
        # children (not cancelled/deleted) BEFORE the branch decision, and
        # derive blocking_children from that SAME filtered list, so a dead
        # child is never named as a blocker either.
        live_kids = [c for c in kids
                    if str(_task_attr(c, "status", "")) not in
                    ("cancelled", "deleted")]
        if live_kids:
            ok_roll, why_roll = epic_rollup_verdict(live_kids)
            # AC-3 (task a646cbd1): epic_rollup_verdict's own reason string
            # is prose ("N child task(s) not done") with no ids — name the
            # specific unfinished child(ren) here from the SAME `live_kids`
            # list it was computed from, so the UI can link, not just read
            # prose.
            blocking_children = [
                {"id": str(_task_attr(c, "id", "")),
                 "title": str(_task_attr(c, "title", ""))}
                for c in live_kids
                if str(_task_attr(c, "status", "")) not in ("done", "cancelled")
            ]
            # A clean roll-up still has to satisfy the ui-artifact tooth (owner:
            # default to visual evidence) — reflect BOTH so READY is never a lie
            # that then fails the approve.
            ui = ui_artifact_gate_reason(getattr(task, "tags", None),
                                         getattr(task, "proof_type", ""),
                                         getattr(task, "completion_proof", ""))
            if ui and has_captured_evidence(task_id):
                ui = ""  # captured evidence satisfies the visual requirement
            ready = bool(ok_roll) and not ui
            reason = why_roll if not ok_roll else (ui or why_roll)
            return {"receipt_ok": ready, "manual_review": True,
                    "receipt_refusal": "" if ready else reason,
                    "blocking_children": blocking_children,
                    "subtree_progress": subtree_progress_counts(s._task_svc, task_id),
                    "receipt": {"adapter": "epic-rollup", "passed": ready,
                                "status": "rollup" if ready else "rollup_blocked",
                                "ended_at": "", "reason": reason}}
    except Exception:
        pass
    # HUMAN-JUDGMENT gate (owner 2026-07-19): a demo/visual/manual oracle has no
    # machine tooth — the person's review IS the sign-off. Present it as READY to
    # approve (a clean, enabled Approve — no override, no failed-machine-receipt),
    # not a failed browser receipt. gate_decide accepts the plain distinct-actor
    # approve for these (see conductor_service green_gate human-judgment branch).
    try:
        from prism_service.services import oracle_spec as _osp
        from prism_service.services.conductor_service import (
            green_gate_artifact_reason, ui_artifact_gate_reason,
            has_captured_evidence)
        pt = str(getattr(task, "proof_type", "") or "").strip().lower()
        if pt in ("demo", "review") or _osp.is_human_judgment(
                _osp.OracleSpec.from_task(task)):
            # Your review IS the sign-off — but a visual/demo gate still needs
            # its VISUAL EVIDENCE captured (owner: default to visual evidence).
            # Reflect the artifact tooth HONESTLY so the card never says READY
            # and then refuses the approve: READY only when the evidence is on
            # file, else an actionable 'attach a screenshot' (not a machine fail).
            #
            # SCOPED TO green_gate ONLY (task 44c7e2d0, owner 2026-08-27): this
            # tooth's own wording ("green_gate requires a captured full-suite-
            # green artifact") names the implement workflow's green_gate step
            # specifically — a code change's tested-and-working proof. It was
            # firing for ANY demo/review-proof-type task regardless of which
            # gate step it was actually at, so promote_to_law's review step
            # (reviewing a drafted TTL rule, never a code/UI change) was
            # blocked demanding a screenshot that step has no reason to have.
            # A workflow with its own real artifact expectation at its own
            # gate step should gain its own scoped check here, not reuse
            # green_gate's.
            art = ""
            if getattr(task, "workflow_step", "") == "green_gate":
                cp = getattr(task, "completion_proof", "")
                art = (green_gate_artifact_reason(cp, "", pt)
                       or ui_artifact_gate_reason(getattr(task, "tags", None),
                                                  pt, cp))
                if art and has_captured_evidence(task_id):
                    art = ""  # a captured screenshot satisfies the requirement
            if art:
                return {"receipt_ok": False, "manual_review": True,
                        "receipt_refusal": art,
                        "receipt": {"adapter": "human", "passed": False,
                                    "status": "needs_visual_evidence",
                                    "ended_at": "", "reason": art}}
            # SHIPPED-NESS DISCLOSURE (task e4e6cd44). This branch used to
            # promise "Approve to release" for tasks whose [task:<id8>]
            # commits are not on origin/main — and the approve path then
            # refused that very click (73 consecutive times on f506ece4).
            # The tooth lives on the service; ask it BEFORE the click.
            ship_reason = ""
            try:
                ship_reason = s._unshipped_gate_reason(task)
            except Exception:
                ship_reason = ""
            if ship_reason:
                from prism_service.services import ship_worker
                if ship_worker.is_enabled():
                    # ON: the click now WORKS — it triggers the landing. Keep
                    # receipt_ok TRUE: LiveGatePanel disables Approve on
                    # `receipt_ok !== true`, so a false receipt here would
                    # disable the very button this feature adds. The unshipped
                    # state is disclosed as an advisory, not a refusal.
                    return {"receipt_ok": True, "receipt_refusal": "",
                            "manual_review": True, "unshipped": True,
                            "ship_on_approve": True,
                            "receipt": {"adapter": "human", "passed": True,
                                        "status": "your_review_then_ship",
                                        "ended_at": "",
                                        "reason": (
                                            "visual/demo gate — your review is "
                                            "the sign-off. Not shipped yet: "
                                            "approving will push the branch, "
                                            "open a PR, wait for CI and merge "
                                            "it, then release the gate.")}}
                # OFF (default): the click genuinely cannot succeed, so say so
                # BEFORE it is spent, and let the button disable honestly.
                return {"receipt_ok": False, "manual_review": True,
                        "unshipped": True, "ship_on_approve": False,
                        "receipt_refusal": ship_reason,
                        "receipt": {"adapter": "human", "passed": False,
                                    "status": "not_shipped", "ended_at": "",
                                    "reason": ship_reason}}
            return {"receipt_ok": True, "receipt_refusal": "",
                    "manual_review": True,
                    "receipt": {"adapter": "human", "passed": True,
                                "status": "your_review", "ended_at": "",
                                "reason": ("visual/demo gate — your review is "
                                           "the sign-off; Approve to release")}}
    except Exception:
        pass
    # UNSHIPPED DISCLOSURE, MACHINE-GRADED LANE (task 8a06e121): the
    # human-judgment branch above already asks _unshipped_gate_reason before
    # every Approve click; this generic EvidenceReceipt branch (proof_type=
    # test with a real pytest oracle) fell straight through to the oracle
    # tooth without ever asking it, so a genuinely-passing receipt read
    # 'ready' here while gate_decide's own green_gate pre-flight refused the
    # very next Approve. The adjudicator owns proof_type=test, so unlike the
    # human-judgment branch this stays a FLAT refusal — no ship_on_approve
    # escape, PRISM_SHIP_ON_APPROVE or not (task stop_if).
    try:
        _ship_reason = s._unshipped_gate_reason(task)
    except Exception:
        _ship_reason = ""
    if _ship_reason:
        return {"receipt_ok": False, "receipt_refusal": _ship_reason,
                "unshipped": True, "ship_on_approve": False,
                "receipt": {"adapter": "human", "passed": False,
                            "status": "not_shipped", "ended_at": "",
                            "reason": _ship_reason}}
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
    # TEST ROWS (task b8703343): one row per pytest id in the task's derived
    # OracleSpec -- the tests that DECIDE this gate -- each carrying the
    # latest matching receipt's provenance. A pure VIEW over
    # OracleSpec.from_task + receipts.jsonl: no new persistence; ids without
    # a matching receipt are listed with passed=None (honestly unevidenced).
    try:
        import re as _re
        from prism_service.services import oracle_spec as osp
        spec = osp.OracleSpec.from_task(task)
        if spec.adapter == osp.ADAPTER_PYTEST and spec.target.strip():
            latest = osp.latest_receipt(project, task_id)
            match = (latest is not None
                     and latest.spec_hash == spec.spec_hash())
            rows = []
            for tid_ in [i for i in _re.split(r"[\s,]+", spec.target) if i]:
                file_, _, rest_ = tid_.partition("::")
                name_ = rest_.split("::")[-1].split("[")[0] if rest_ else ""
                href_ = (f"/artifact?focus={file_}"
                         + (f"&symbol={name_}" if name_ else ""))
                rows.append({
                    "id": tid_,
                    "label": (file_.rsplit("/", 1)[-1]
                              + (f"::{name_}" if name_ else "")),
                    "href": href_,
                    "passed": bool(latest.passed) if match else None,
                    "status": latest.status if match else "not-run",
                    "ended_at": latest.ended_at if match else "",
                    "receipt_job_id": latest.job_id if match else "",
                })
            if rows:
                out["tests"] = rows
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


@router.post("/resume/release")
def resume_release(project: str = Query("default"),
                   body: dict = Body(...)) -> dict:
    """Release a task the resume actuator PARKED, back to the drive.

    The actuator spends a retry budget and writes `status=blocked` when a
    step keeps failing. Nothing could undo that (task 5227a646): the
    budget reset lived only inside a successful dispatch, so a parked task
    stayed parked even once the cause was fixed, and flipping it to
    `in_progress` by hand just let the next sweep re-park it. This is the
    "the cause is fixed, try again" affordance — the one place the budget
    legitimately resets.
    """
    task_id = (body or {}).get("task_id") or ""
    if not task_id:
        raise HTTPException(422, "task_id required")
    from prism_service.services import resume_actuator

    actor = (body or {}).get("actor") or (body or {}).get("session_id") or "human"
    return resume_actuator.release(project, task_id, actor=str(actor))


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
