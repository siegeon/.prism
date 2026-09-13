"""Stalled-drive auto-resume actuator (task 7a72ebcb).

`ConductorService.activity_for` already DETECTS the exact state the owner
complained about (task_motion_s stale, session_quiet_s stale/absent, no
drive_heartbeat) -- see tests/test_activity_state.py's own
`test_stalled_both_stale`. What was missing is the ACTUATOR: on that
detection, something dispatches a real driver for the task instead of
waiting for a human to notice in chat and relaunch `implement` by hand.

Mirrors `task_runner.py` / `gate_adjudicator.py`: env-gated, OFF by
default. Reuses `task_runner`'s own invoke/report plumbing rather than
duplicating it (BUILD_TOOLS, proof routing, budget caps) -- this module
adds only the eligibility check, the dispatch-time heartbeat + attributable
history row, and the retry budget (`resume_attempts_data`).

NEVER decides a gate: eligibility itself excludes any task parked at a
gate step, and this module contains no gate-approving call of its own --
that stays a distinct seat's job.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Optional

from prism_service.services import system_activity

DEFAULT_INTERVAL_S = 0  # OFF unless an environment explicitly opts in
SEAT = "prism-resume-actuator"  # distinct-actor identity on every report

# History-row actions this actuator writes -- distinct from advance_task or
# a gate-outcome call (the tile's own motion clock, conductor_service.
# _task_motion_s) so a dispatch is never mistaken for a real conductor
# transition.
DISPATCH_ACTION = "resume_actuator_dispatch"
PARKED_ACTION = "resume_actuator_parked"

DEFAULT_MAX_RETRIES = 3

# ABSOLUTE CEILING ON DISPATCHES PER TASK, which no reset can lift.
# 7.13.233 taught the sweep to reset a spent budget when a conductor
# transition landed after the last charged attempt — right for a task
# that genuinely moved on, but it removed the only backstop for a task
# that OSCILLATES. Live on 338f7810 (2026-09-05): the drive advanced,
# was refused, rewound and advanced again, so transitions never stopped,
# the budget reset every sweep, and the seat dispatched 37 times over
# 4h40m without ever parking. The owner saw it as "stuck in some type of
# cycle" with "hundreds of attempts".
# The per-pass budget still governs the normal case. This counts every
# dispatch this seat has EVER made for the task, from durable history, so
# an oscillating task stops even while it keeps producing transitions.
DEFAULT_MAX_TOTAL_DISPATCHES = 12

# A dispatch refusal in this list never spent by the WORK -- the drive
# never got a turn, so it must never cost a retry attempt (ops incident
# task a65c66e5, 2026-09-13): a reaped worktree made every tick fail
# "workspace unavailable, refusing to start (fail closed): ..." and the
# old code charged the budget on every refusal alike, spending all 3
# attempts on an outage that 7.13.330 later fixed for good -- the task
# then sat parked "for a human" with nothing for a human to actually do.
# Only a refusal that matches one of these markers is infrastructure; any
# other refusal (a dispatch that ran and failed, or a genuine step
# failure) still charges as before.
_INFRA_REFUSAL_MARKERS = (
    "workspace unavailable",
    "fail closed",
    "engine unreachable",
    "engine slot busy",
    "daemon restart",
    "git transport",
    "already driving",
)

# The prefix `_park` and `release()` both recognise as THIS seat's own
# park, distinct from a human's manual block or another seat's park.
_PARK_PREFIX = "resume-actuator:"
# `_park`'s message wraps these tags in backtick inline-code spans so
# ste.normalize's own semicolon-to-sentence-break rewrite (task/memory
# writes all pass through it -- CLAUDE.md's ASD-STE100 doctrine) never
# capitalises or otherwise mutates them: a protected span is copied
# through byte-for-byte. The regexes below tolerate the backticks and
# any trailing punctuation either way, so a legacy (pre-backtick,
# pre-STE) park still parses.
_CLASS_RE = re.compile(r"class=(\w+)", re.IGNORECASE)
_VERSION_RE = re.compile(r"parked_version=(\S+)", re.IGNORECASE)


def _refusal_class(reason: str) -> str:
    """'infra' for a dispatch refusal caused by infrastructure (workspace,
    engine, daemon, git transport) rather than the work itself; 'work'
    otherwise. Only a 'work' refusal ever costs a retry attempt."""
    r = (reason or "").lower()
    return "infra" if any(m in r for m in _INFRA_REFUSAL_MARKERS) else "work"


def _park_meta(reason: str) -> tuple[str, str]:
    """(class, parked_version) tags on a park reason THIS seat wrote, or
    ('unknown', '') for one written before this classification existed --
    exactly task a65c66e5's real blocked_reason, which names no cause and
    no PRISM_VERSION at all."""
    reason = reason or ""
    m_cls = _CLASS_RE.search(reason)
    m_ver = _VERSION_RE.search(reason)
    cls = m_cls.group(1).lower() if m_cls else "unknown"
    version = m_ver.group(1).rstrip("`.,;:)") if m_ver else ""
    return cls, version


def _max_total_dispatches() -> int:
    raw = os.environ.get("PRISM_RESUME_ACTUATOR_MAX_TOTAL", "")
    try:
        return max(1, int(raw)) if raw.strip() else DEFAULT_MAX_TOTAL_DISPATCHES
    except ValueError:
        return DEFAULT_MAX_TOTAL_DISPATCHES


def _interval_s() -> int:
    raw = os.environ.get("PRISM_RESUME_ACTUATOR_INTERVAL", "")
    try:
        return int(raw) if raw.strip() else DEFAULT_INTERVAL_S
    except ValueError:
        return DEFAULT_INTERVAL_S


def _max_retries() -> int:
    raw = os.environ.get("PRISM_RESUME_ACTUATOR_MAX_RETRIES", "")
    try:
        return max(1, int(raw)) if raw.strip() else DEFAULT_MAX_RETRIES
    except ValueError:
        return DEFAULT_MAX_RETRIES


def _log(msg: str) -> None:
    print(f"[resume-actuator] {msg}", file=sys.stderr, flush=True)


def is_enabled() -> bool:
    """True when this environment opted into the seat (interval > 0)."""
    return _interval_s() > 0


def _scores_db_for(project: str) -> str:
    from prism_service.project_context import get_project

    return str(get_project(project)._data_dir / "scores.db")


def is_stalled_and_eligible(task, cond, phase_progress: dict) -> bool:
    """True iff `task` is exactly the state activity_for renders 'stalled'
    for, and its current step is not a gate. Reads task_motion_s /
    session_quiet_s / heartbeat exactly as activity_for does (AC-1) by
    calling that same function -- this is never a reimplementation of its
    logic."""
    from prism_service.services.conductor_service import ConductorService

    step_id = getattr(task, "workflow_step", "") or ""
    if step_id:
        step = ConductorService._step_by_id(step_id)
        if step is None or step.get("type") == "gate":
            return False
    act = cond.activity_for(task, phase_progress)
    return act.get("state") == "stalled"


def eligible_task(project: str) -> Optional[str]:
    """The id of one NEW stalled, non-gate, in_progress task in `project`
    this actuator may pick up, or None. A task the owner blocked, or one
    parked at a gate, is never returned (AC-6)."""
    from prism_service.project_context import get_project

    ctx = get_project(project)
    cond = ctx.conductor_svc
    for t in ctx.task_svc.list(status="in_progress"):
        if not getattr(t, "workflow_step", ""):
            continue
        phase = cond.phase_progress(t.id)
        if is_stalled_and_eligible(t, cond, phase):
            return t.id
    return None


def _open_retry_task_id(project: str) -> Optional[str]:
    """A task this actuator already claimed (an open, unresolved attempt
    budget) -- retried directly on the next sweep without re-checking
    'stalled', since the heartbeat THIS actuator just wrote is exactly what
    would otherwise mask it as 'driving' rather than 'stalled'."""
    from prism_service.project_context import get_project
    from prism_service.services import resume_attempts_data as rad
    from prism_service.services.conductor_service import ConductorService

    ctx = get_project(project)
    scores_db = _scores_db_for(project)
    for t in ctx.task_svc.list(status="in_progress"):
        step_id = getattr(t, "workflow_step", "") or ""
        step = ConductorService._step_by_id(step_id) if step_id else None
        if step is not None and step.get("type") == "gate":
            continue
        if rad.attempt_count(scores_db, t.id) > 0:
            return t.id
    return None


def _park(project: str, task_id: str, attempts: int, max_retries: int) -> dict:
    from prism_service.__version__ import PRISM_VERSION
    from prism_service.project_context import get_project
    from prism_service.services import resume_attempts_data as rad

    ctx = get_project(project)
    scores_db = _scores_db_for(project)
    last = rad.last_reason(scores_db, task_id) or "no reason recorded"
    # By construction this only ever fires on charged (class=work) attempts
    # -- an infra refusal never reaches here (see _refusal_class /
    # _no_advance) -- so the class is always named explicitly, and a later
    # sweep's re-arm check (`_rearm_once`) can tell this genuine step
    # failure apart from an infrastructure one and leave it for a person.
    # Backtick-wrapped: an inline-code span is a PROTECTED span for
    # ste.normalize (every task write passes through it), so these tags
    # survive byte-for-byte instead of having their own semicolon rewrite
    # capitalise "class=work" into an unmatchable "Class=work".
    reason = (f"{_PARK_PREFIX} retry budget spent ({attempts}/{max_retries}). "
              f"`class=work`. Last reason: {last}. "
              f"`parked_version={PRISM_VERSION}`. This was a real step "
              "failure, not an infrastructure refusal. It needs a "
              "person's review. Call resume_actuator.release() once the "
              "cause is fixed.")
    ctx.task_svc.update(task_id, status="blocked", blocked_reason=reason)
    ctx.task_svc.record_history(task_id, action=PARKED_ACTION,
                                details=reason, actor=SEAT)
    return {"ok": False, "task_id": task_id, "parked": True, "reason": reason}


RELEASED_ACTION = "resume_actuator_released"

# Conductor transitions — the only rows that mean the WORK moved. A
# dispatch row or a heartbeat says a seat tried, never that it got
# anywhere, so neither may clear a retry budget.
_ADVANCE_ACTIONS = ("advance_task", "gate_decide")


def _advanced_since(project: str, task_id: str, since_iso: str) -> bool:
    """True when a conductor transition landed AFTER `since_iso`.

    Answers "has the work moved since we last charged an attempt?" from
    server-stamped history, so a budget spent at an earlier step cannot
    park a task that another seat has already advanced. Fails CLOSED:
    with no timestamp, an unparsable one, or any read error it returns
    False and the park stands — this can only ever spare a task that
    demonstrably moved."""
    if not since_iso:
        return False
    from datetime import datetime, timezone

    def _parse(raw: str):
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts

    since = _parse(since_iso)
    if since is None:
        return False
    try:
        from prism_service.project_context import get_project

        rows = get_project(project).task_svc.history(task_id) or []
    except Exception:
        return False
    for r in rows:
        if str(getattr(r, "action", "") or "") not in _ADVANCE_ACTIONS:
            continue
        ts = _parse(str(getattr(r, "timestamp", "") or ""))
        if ts is not None and ts > since:
            return True
    return False


def release(project: str, task_id: str, actor: str = "human",
           note: str = "") -> dict:
    """Release a task this seat PARKED, back to the drive (task 5227a646).

    `_park` spends the retry budget and writes `status=blocked`, and until
    now NOTHING could undo that: `reset_attempts` was reachable only from
    inside a successful dispatch, so a parked task stayed parked forever
    even after the cause was fixed — flipping it back to `in_progress` by
    hand just let the next 180 s sweep re-park it on the same spent
    budget. Observed on ce471e06 (2026-09-04).

    This is the human's "the cause is fixed, try again" signal, so it is
    the one place the budget legitimately resets: clear the attempts, lift
    a park-shaped `blocked` back to `in_progress`, and record WHO released
    it. A task blocked for any other reason keeps its own blocked_reason
    and is left alone — this releases the actuator's park, never a real
    dependency block.

    `note`, when given, replaces the default "released by ..." wording in
    the history row's details -- used by `_rearm_once` so an AUTOMATIC
    re-arm reads distinctly from a human's manual release (task a65c66e5:
    "the cause class was infrastructure and it is now demonstrably gone",
    never a bare "released by resume-actuator")."""
    from prism_service.project_context import get_project
    from prism_service.services import resume_attempts_data as rad

    ctx = get_project(project)
    task = ctx.task_svc.get(task_id)
    if task is None:
        return {"ok": False, "task_id": task_id, "reason": "no such task"}

    scores_db = _scores_db_for(project)
    attempts = rad.attempt_count(scores_db, task_id)
    rad.reset_attempts(scores_db, task_id)

    status = getattr(task, "status", "") or ""
    reason = getattr(task, "blocked_reason", "") or ""
    parked_by_seat = status == "blocked" and "resume-actuator:" in reason
    if parked_by_seat:
        ctx.task_svc.update(task_id, status="in_progress", blocked_reason="")

    ctx.task_svc.record_history(
        task_id, action=RELEASED_ACTION,
        details=(note or
                 (f"released by {actor}; retry budget reset "
                  f"(was {attempts}); "
                  + ("unparked to in_progress" if parked_by_seat
                     else f"status left as {status or 'unknown'}"))),
        actor=actor)
    return {"ok": True, "task_id": task_id, "attempts_cleared": attempts,
            "unparked": parked_by_seat}


def _workspace_now_resolves(task_id: str) -> bool:
    """True when `ensure_workspace` succeeds for `task_id` right now --
    the live, un-mocked check for "the infrastructure cause is gone"
    (task a65c66e5: a reaped worktree recovers via its own surviving
    branch since 7.13.330). Fails closed: any error means the cause is
    still live."""
    from prism_service.services import task_workspace

    try:
        task_workspace.ensure_workspace(task_id)
        return True
    except Exception:
        return False


def _rearm_once(project: str) -> Optional[dict]:
    """Auto-lift the FIRST park this seat wrote for an infrastructure (or
    legacy, unclassified) cause that is now demonstrably gone -- never a
    park this seat tagged `class=work` (a genuine step failure, which
    stays for a person), and never a park another seat or a human wrote.

    A "human release" was the only way to undo `_park` before this (see
    `release`'s own docstring) -- for a park whose ENTIRE cause was
    infrastructure, that is exactly the "no gate or park is the human's"
    shape the owner has repeatedly flagged as a defect (task a65c66e5,
    ops incident 2026-09-13: 3/3 retries spent on "workspace unavailable,
    refusing to start (fail closed)" while the underlying bug was already
    fixed in 7.13.330, and nothing could lift the resulting park short of
    a human noticing and calling `release()` by hand).

    Matches only `_park`'s own "retry budget spent" wording -- deliberately
    NOT `_park_looping`'s ceiling message (task 338f7810's oscillation
    backstop), which must never auto-lift regardless of class."""
    from prism_service.__version__ import PRISM_VERSION
    from prism_service.project_context import get_project

    ctx = get_project(project)
    for t in ctx.task_svc.list(status="blocked"):
        reason = getattr(t, "blocked_reason", "") or ""
        if not reason.startswith(_PARK_PREFIX) or "retry budget spent" not in reason:
            continue
        cls, parked_version = _park_meta(reason)
        if cls == "work":
            continue
        version_changed = bool(parked_version) and parked_version != PRISM_VERSION
        workspace_ok = _workspace_now_resolves(t.id)
        if not (version_changed or workspace_ok):
            continue
        cause = (f"{cls} cause cleared (parked_version="
                 f"{parked_version or 'unknown'}, now={PRISM_VERSION}, "
                 f"workspace_ok={workspace_ok})")
        result = release(project, t.id, actor=SEAT,
                         note=f"re-armed after {cause}")
        if result.get("ok"):
            return {"ok": True, "task_id": t.id, "rearmed": True,
                    "reason": cause}
    return None


def _total_dispatches(project: str, task_id: str) -> int:
    """Dispatches this seat has made SINCE THE LAST HUMAN RELEASE, from
    durable history. Survives a daemon restart, and no automatic budget
    reset can lower it — that is the point: it is the backstop for a task
    that oscillates (338f7810: 37 dispatches over 4h40m, advancing and
    rewinding the whole time).

    COUNTED FROM THE LAST RELEASE, not from the beginning of time (task
    1bcb2b24, 2026-09-08). `_park_looping` writes "Parked for a person",
    and `release` calls itself the human's "the cause is fixed, try again"
    signal — but release only cleared the per-pass attempt budget, so the
    very next sweep read the same all-time total, hit the ceiling again and
    re-parked. A task that reached this ceiling was therefore unreachable
    FOR EVER, by anyone, however thoroughly a person fixed the cause; the
    park's own message promised a remedy that did not exist.

    An explicit, audited human release is precisely the event this backstop
    should yield to: it is a person stating the oscillation has a known
    cause and that cause is now fixed. Oscillation with nobody watching
    still parks at the ceiling, unchanged, because only a RELEASED_ACTION
    row moves the start of the count."""
    try:
        from prism_service.project_context import get_project

        rows = get_project(project).task_svc.history(task_id) or []
    except Exception:
        return 0
    start = 0
    for i, r in enumerate(rows):
        if str(getattr(r, "action", "") or "") == RELEASED_ACTION:
            start = i + 1
    return sum(1 for r in rows[start:]
               if str(getattr(r, "action", "") or "") == DISPATCH_ACTION)


def _park_looping(project: str, task_id: str, total: int,
                  ceiling: int) -> dict:
    """Park a task this seat has re-driven past its absolute ceiling.

    Distinct from _park's spent-budget message on purpose: a person
    reading this needs to know the task kept MOVING and still never
    finished, which is a different problem from a step that never
    advanced once."""
    from prism_service.project_context import get_project

    ctx = get_project(project)
    reason = (f"resume-actuator: {total} dispatches for this task, at the "
              f"ceiling of {ceiling}. The task kept changing step without "
              "reaching a terminal state, so re-driving it is not making "
              "progress. Parked for a person.")
    ctx.task_svc.update(task_id, status="blocked", blocked_reason=reason)
    ctx.task_svc.record_history(task_id, action=PARKED_ACTION,
                                details=reason, actor=SEAT)
    return {"ok": False, "task_id": task_id, "parked": True,
            "looping": True, "reason": reason}


def _dispatch_count(task_svc, task_id: str) -> int:
    """How many times this seat has dispatched THIS task, from durable
    history. Strictly non-decreasing and restart-safe, which is what
    drive_heartbeat's monotonic work_units guard needs to keep accepting
    this seat's beats as real progress."""
    try:
        rows = task_svc.history(task_id) or []
    except Exception:  # noqa: BLE001 - liveness is best-effort, never fatal
        return 1
    return sum(1 for r in rows
               if getattr(r, "action", "") == DISPATCH_ACTION) + 1


def dispatch_once(project: str, task_id: str) -> dict:
    """Dispatch one driver tick for `task_id`: claim the task's lease FIRST,
    then fire the attributable history row and heartbeat (AC-2/AC-4 -- the
    tile moves off 'stalled' the instant a REAL dispatch fires), then the
    SAME invoke/report plumbing task_runner.py uses. A report that genuinely
    advances the workflow_step resets the retry budget; anything else
    increments it (AC-5).

    Mirrors task_runner._run_one_step's own ordering (task b490fabc,
    2026-09-11): a beat recorded BEFORE the claim check is a beat for work
    that never started. Live incident on b490fabc showed both seats reading
    the OTHER seat's pre-check beat as "a live driver" and yielding to it —
    two seats, each deferring to the other's ghost, for a whole 30-minute
    lease, with no agent_runs row and no claude -p process behind either
    beat. A deferred attempt (the claim already held by another driver)
    therefore writes NO heartbeat and NO DISPATCH_ACTION history row: it
    never charges toward the 12-dispatch total ceiling either, since that
    ceiling counts DISPATCH_ACTION rows."""
    from prism_service.api import conductor_flow as flow
    from prism_service.project_context import get_project
    from prism_service.services import drive_heartbeat
    from prism_service.services import resume_attempts_data as rad
    from prism_service.services import task_workspace
    from prism_service.services.task_runner import (
        BUILD_TOOLS, _max_budget_usd, _max_turns, _route_proof,
    )

    ctx = get_project(project)
    task_svc = ctx.task_svc
    scores_db = _scores_db_for(project)

    def _no_advance(reason: str, **extra) -> dict:
        # A refusal classified as infrastructure never charges the retry
        # budget -- the drive never got a turn, so it is never evidence
        # the WORK failed (task a65c66e5: "workspace unavailable, refusing
        # to start (fail closed)" spent all 3 attempts on an outage that
        # 7.13.330 fixed for good, and parked the task for a human with
        # nothing for a human to actually do).
        cls = _refusal_class(reason)
        if cls != "infra":
            rad.record_attempt(scores_db, task_id, reason=reason)
        return {"ok": False, "task_id": task_id, "reason": reason,
                "refusal_class": cls, **extra}

    started = flow.flow_start(
        flow.Ident(task_id=task_id, session_id=SEAT), project=project)
    if not started.get("ok"):
        return _no_advance(started.get("error") or "flow_start refused")
    job = started.get("job")
    if not job or job.get("kind") == "gate":
        return _no_advance("no eligible agent job (gate or terminal)")

    ws = task_workspace.workspace_for(task_id) or {}
    work_dir = ws.get("path")
    if not work_dir:
        return _no_advance("no workspace on file for task", step=job["step"])

    # EVERY DRIVER TAKES THE SAME LEASE (task 1bcb2b24). 7.13.212 wired only
    # task_runner, and this seat kept spawning claude_cli into the SAME task
    # worktree -- on 2026-08-30 it did so twice while another driver held the
    # claim, once truncating that driver's red-step commit to a stub. A lock
    # that one seat honours is not a lock. This is the ticket's own named
    # misfire, shipped: "the lock covers only task_runner, so resume_actuator,
    # ship_worker and an operator agent still enter."
    #
    # ACQUIRE BEFORE ANY BEAT (task b490fabc, 2026-09-11). This used to write
    # the DISPATCH_ACTION row and heartbeat ABOVE, before this claim check --
    # so a claim held by another driver still landed a heartbeat/history row
    # for work that never ran, and the two seats read each other's pre-check
    # beats as a live driver forever (see docstring). Beat only once the
    # lease is actually held.
    from prism_service.services import task_runner as _tr

    claim = _tr._claim_service(project)
    claim_id = None
    if claim is not None:
        claim_id = claim.acquire(task_id, holder_id=SEAT,
                                 ttl_s=_tr._step_timeout_s(job["step"]))
        if claim_id is None:
            holder = claim.holder_of(task_id) or "another driver"
            # A HELD LEASE IS NOT A FAILED ATTEMPT (task ce471e06,
            # 2026-09-04). Spending the retry budget here killed healthy
            # long-running steps: verify_green_state takes ~25 min and
            # holds a 45 min lease, while this seat sweeps every 180 s, so
            # three bounces off a driver that was working normally spent
            # the whole budget and PARKED the task for a human. Another
            # driver holding the lease is evidence that work IS happening
            # — the opposite of the stall this seat exists to rescue — so
            # defer without charging an attempt and pick it up on a later
            # sweep if it really does go quiet. NO heartbeat, NO
            # DISPATCH_ACTION row: this attempt never ran (task b490fabc).
            return {"ok": False, "task_id": task_id, "step": job["step"],
                    "deferred": True,
                    "reason": f"already driving: held by {holder}"}

    # THE ENGINE SLOT, CHECKED BEFORE ANY BEAT (task 8ddbba7f, 2026-09-13).
    # Mirrors the claim check just above for the identical reason: a beat
    # written for a dispatch about to be refused anyway is a ghost a LATER,
    # unrelated attempt could read as a live driver and defer to
    # needlessly (the exact b490fabc shape, for a different gate).
    # dispatch_guard.try_begin below is still the authoritative, atomic
    # reservation right before the real invoke; this is the same
    # no-side-effect pre-check task_runner/dispatch use. NO heartbeat, NO
    # DISPATCH_ACTION row, and no attempt spent -- this seat did not fail,
    # it deferred to a slot it cannot have right now.
    from prism_service.services import dispatch_guard as _dgu

    busy = _dgu.engine_slot_reason(project, exclude_task_id=task_id)
    if busy:
        if claim is not None:
            claim.release(claim_id)
        return {"ok": False, "task_id": task_id, "step": job["step"],
                "deferred": True, "reason": busy}

    # THE LEASE IS HELD -- this is a real dispatch. Beat and record it now,
    # not before (task b490fabc).
    task_svc.record_history(task_id, action=DISPATCH_ACTION,
                            details=f"seat={SEAT}; step={job['step']}",
                            actor=SEAT)
    # work_units MUST STRICTLY INCREASE ACROSS DISPATCHES. record_heartbeat
    # is monotonic: a beat repeating the previously stored counter does NOT
    # advance last_progress_at, so a hardcoded 1 refreshed this seat's own
    # liveness exactly once, on the very first dispatch, and never again.
    # This docstring promises "the tile moves off 'stalled' the instant
    # dispatch fires" -- with a constant it moved off once and the task read
    # stalled again 180s later, every time, no matter how often the seat
    # rescued it. The dispatch count from this task's own history always
    # increases and survives a daemon restart.
    _beats = _dispatch_count(task_svc, task_id)
    drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": job["step"] or "unknown", "elapsed_s": 0,
        "last_tool": "resume_actuator_dispatch",
        "work_units": max(1, _beats),
        "driver": SEAT,
    })

    from prism_service.inference import claude_cli

    # Route through the SAME plan-aware helpers task_runner uses so declared
    # node plans (model, turns, budget, prompt, tools) reach this seat's invoke.
    # For steps with no declared plan, this returns defaults. facts=[] here
    # because this seat runs no premise gather (task_runner has the same branch
    # in _declared_agentic_prompt).
    plan = _tr._node_plan(project, job["step"])
    task_for_prompt = task_svc.get(task_id) if plan else None
    narrow_prompt = _tr._declared_agentic_prompt(
        job["step"], task_for_prompt, [], plan=plan)
    prompt = narrow_prompt or job["instructions"]
    # after_kill: this seat re-drove d5808cd1's draft_story twelve times, and
    # the last of them still spent a full 900 s on a step whose previous
    # outcome was already a budget kill. The two seats must agree here too.
    budget = _tr._invoke_budget(
        job["step"], plan, narrow=bool(narrow_prompt),
        after_kill=_tr._last_outcome_was_a_kill(
            task_svc, task_id, job["step"]))

    # THE CHOKEPOINT (task ab9166d5 incident, dispatch_guard.py): re-check,
    # fresh, right before the GPU actually spends anything -- the claim
    # above only proves no OTHER driver holds this task, it says nothing
    # about whether this task is still supposed to be driven at all (a
    # park can land between the stall recheck above and here). This is
    # also the one place a REAL dispatch is counted, shared with
    # task_runner, so the ceiling it enforces cannot disagree with what
    # actually ran -- unlike this seat's own attempt/total-dispatch
    # bookkeeping above, which only ever saw ITS OWN attempts.
    from prism_service.services import dispatch_guard
    ticket, refusal = dispatch_guard.try_begin(project, task_id, job["step"], SEAT)
    if ticket is None:
        if claim is not None:
            claim.release(claim_id)
        # THE RACE THE PRE-CHECK ABOVE CANNOT CLOSE: a slot that looked
        # free a moment ago may already be taken by the time try_begin's
        # atomic check runs. Same rule as the pre-check -- a busy engine
        # is not this task's fault, so it must not spend the retry budget
        # (task 8ddbba7f).
        if refusal and "engine slot busy" in refusal:
            return {"ok": False, "task_id": task_id, "step": job["step"],
                    "deferred": True, "reason": refusal}
        return _no_advance(refusal or "dispatch refused", step=job["step"])

    try:
        result = claude_cli.invoke(
            prompt, work_dir=work_dir, plugin_dir=work_dir,
            allowed_tools=() if narrow_prompt else BUILD_TOOLS,
            project=project,
            purpose=f"resume-actuator@{job['step']}#{task_id[:8]}",
            **budget)
    except Exception as exc:
        dispatch_guard.end_dispatch(ticket)
        if claim is not None:
            claim.release(claim_id)
        return _no_advance(f"claude_cli invocation failed: {exc}",
                           step=job["step"])
    dispatch_guard.end_dispatch(ticket)

    proof = (result.final_text() or "").strip()
    step_id = job["step"]
    # GRACEFUL BUDGET STOP (7.13.102 fixed this in task_runner._run_one_step
    # and this seat never inherited it — the retry seat therefore threw away
    # a COMPLETE step report every time a post-hoc --max-budget-usd /
    # --max-turns ceiling raised the exit code after the model's own turn
    # ended normally. Three such retries spend the retry budget and park the
    # task for a human, which is how ce471e06 blocked at write_failing_tests
    # on 2026-09-04 with three identical "exit=1, no usable output" rows.
    # The two seats must agree: a graceful stop with real proof PASSES, an
    # empty proof fails, and a genuine crash/auth/mid-turn truncation still
    # fails — and now says which of the two it was.
    graceful = False
    try:
        graceful = bool(result.graceful_budget_stop())
    except Exception:
        graceful = False
    if proof and (result.exit_code == 0 or graceful):
        _route_proof(task_svc, task_id, step_id, proof)
        outcome: object = "pass"
    elif not proof:
        outcome = {"ok": False,
                   "reason": f"exit={result.exit_code}, no usable output"}
    else:
        outcome = {"ok": False,
                   "reason": f"exit={result.exit_code}, non-graceful "
                             "failure (crash/auth/truncated mid-turn)"}

    if claim is not None:
        claim.release(claim_id)

    usage = getattr(result, "usage", None)
    usage = dict(usage) if isinstance(usage, dict) and usage else None

    report = flow.flow_report(flow.Ident(
        task_id=task_id, session_id=SEAT, outcome=outcome,
        expected_step=step_id, usage=usage,
        model=(usage or {}).get("model") or None), project=project)

    if report.get("ok"):
        rad.reset_attempts(scores_db, task_id)
    else:
        # A dispatch that actually ran (claude_cli.invoke was called) and
        # still failed is always work-class -- the drive got its turn, so
        # this always charges, unlike the infra-classified refusals in
        # `_no_advance` above that never reached an invoke at all. Record
        # the real failure text so `_park` can name it instead of a bare
        # "parked for a human" (task a65c66e5).
        fail_reason = ((outcome.get("reason") if isinstance(outcome, dict)
                       else None) or report.get("error")
                      or "flow_report refused")
        rad.record_attempt(scores_db, task_id, reason=fail_reason)

    return {"ok": bool(report.get("ok")), "task_id": task_id,
            "step": step_id, "run_id": getattr(result, "run_id", None),
            "report": report}


def sweep_once_for(project: str) -> Optional[dict]:
    """One pass over `project`: continue an open retry (bypassing the
    'stalled' recheck -- see `_open_retry_task_id`), park it if its budget
    is spent, or else dispatch one newly-stalled task. Returns None when
    nothing was eligible."""
    from prism_service.services import resume_attempts_data as rad
    from prism_service.services import task_runner as _runner

    # A DEAD ENGINE IS NOT A STALL (task b490fabc, 2026-09-11). This seat
    # dispatches on its own path, so the runner's breaker does not cover it,
    # and a retry here would spend budget and park the task on an outage.
    if _runner._engine_unreachable():
        return None

    # RE-ARM BEFORE ANYTHING ELSE (task a65c66e5, 2026-09-13): a task this
    # seat parked purely on an infrastructure refusal must not sit blocked
    # forever once that cause is gone -- checked first, and if it fires
    # this tick's one action is the re-arm itself (mirrors sweep_once's own
    # "at most one task advances per tick" discipline), never also a
    # dispatch in the same pass.
    rearmed = _rearm_once(project)
    if rearmed is not None:
        return rearmed

    scores_db = _scores_db_for(project)
    max_retries = _max_retries()

    task_id = _open_retry_task_id(project)
    if task_id is not None:
        # THE CEILING NO RESET CAN LIFT. Checked BEFORE the per-pass
        # budget, because that budget resets on every transition and an
        # oscillating task never stops producing them (338f7810: 37
        # dispatches over 4h40m, advancing and rewinding the whole time).
        total = _total_dispatches(project, task_id)
        ceiling = _max_total_dispatches()
        if total >= ceiling:
            return _park_looping(project, task_id, total, ceiling)
        attempts = rad.attempt_count(scores_db, task_id)
        if attempts >= max_retries:
            # A BUDGET ONLY COUNTS AGAINST WORK THAT HAS NOT MOVED (task
            # 338f7810, 2026-09-04). dispatch_once resets the count when
            # ITS OWN report advances the task — but ANY seat may advance
            # it, and a count left over from an earlier step then parks a
            # task that is making progress. Live: review_previous_notes
            # advanced at 00:05:10 and this seat parked the task 52 s
            # later, on three attempts spent at the PREVIOUS step. Same
            # shape as the rewind/stall-budget defect — a counter that
            # outlives the work it was counting.
            if _advanced_since(project, task_id,
                               rad.last_attempt_at(scores_db, task_id)):
                rad.reset_attempts(scores_db, task_id)
                return dispatch_once(project, task_id)
            return _park(project, task_id, attempts, max_retries)
        return dispatch_once(project, task_id)

    task_id = eligible_task(project)
    if task_id is None:
        return None
    return dispatch_once(project, task_id)


def sweep_once() -> Optional[dict]:
    """One pass over every project: dispatch/park the first eligible task
    found and stop -- AT MOST one task advances per tick (mirrors
    task_runner.sweep_once)."""
    from prism_service.project_context import get_all_projects

    for pid in get_all_projects():
        try:
            res = sweep_once_for(pid)
        except Exception as exc:
            _log(f"{pid}: sweep failed: {exc}")
            continue
        if res is not None:
            _log(f"{pid}: {res}")
            return res
    return None


def _loop(interval_s: int) -> None:
    _log(f"started; interval={interval_s}s")
    while True:
        try:
            with system_activity.pass_("resume_actuator", "*", "sweep_once"):
                sweep_once()
        except Exception as exc:
            _log(f"sweep error: {exc}")
        time.sleep(interval_s)


def start_resume_actuator() -> threading.Thread | None:
    """Spawn the actuator daemon thread, unless disabled via
    PRISM_RESUME_ACTUATOR_INTERVAL<=0 (the default). Mirrors
    gate_adjudicator.start_gate_adjudicator / task_runner.start_task_runner
    -- until this is called from main.py's startup, `sweep_once` is only
    ever invoked by its own tests, so the eligibility/dispatch/retry-budget
    logic they pin never actually runs against a live task (found auditing
    task 7a72ebcb's own green_gate: no caller of sweep_once/sweep_once_for
    existed anywhere outside tests/unit/test_resume_actuator_stall_dispatch.py)."""
    interval = _interval_s()
    if interval <= 0:
        _log("disabled (default OFF; set PRISM_RESUME_ACTUATOR_INTERVAL="
             "<seconds> to opt this environment in)")
        return None
    t = threading.Thread(target=_loop, args=(interval,),
                         name="prism-resume-actuator", daemon=True)
    t.start()
    return t
