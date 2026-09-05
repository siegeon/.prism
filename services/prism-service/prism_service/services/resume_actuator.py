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
import sys
import threading
import time
from typing import Optional

DEFAULT_INTERVAL_S = 0  # OFF unless an environment explicitly opts in
SEAT = "prism-resume-actuator"  # distinct-actor identity on every report

# History-row actions this actuator writes -- distinct from advance_task or
# a gate-outcome call (the tile's own motion clock, conductor_service.
# _task_motion_s) so a dispatch is never mistaken for a real conductor
# transition.
DISPATCH_ACTION = "resume_actuator_dispatch"
PARKED_ACTION = "resume_actuator_parked"

DEFAULT_MAX_RETRIES = 3


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
    from prism_service.project_context import get_project

    ctx = get_project(project)
    reason = (f"resume-actuator: retry budget spent "
              f"({attempts}/{max_retries}) — parked for a human")
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


def release(project: str, task_id: str, actor: str = "human") -> dict:
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
    dependency block."""
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
        details=(f"released by {actor}; retry budget reset "
                 f"(was {attempts}); "
                 + ("unparked to in_progress" if parked_by_seat
                    else f"status left as {status or 'unknown'}")),
        actor=actor)
    return {"ok": True, "task_id": task_id, "attempts_cleared": attempts,
            "unparked": parked_by_seat}


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
    """Dispatch one driver tick for `task_id`: an attributable history row
    and a heartbeat fire FIRST (AC-2/AC-4 -- the tile moves off 'stalled'
    the instant dispatch fires, whatever the outcome), then the SAME
    invoke/report plumbing task_runner.py uses. A report that genuinely
    advances the workflow_step resets the retry budget; anything else
    increments it (AC-5)."""
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

    task = task_svc.get(task_id)
    step_id = getattr(task, "workflow_step", "") or ""

    task_svc.record_history(task_id, action=DISPATCH_ACTION,
                            details=f"seat={SEAT}; step={step_id}",
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
        "task_id": task_id, "step": step_id or "unknown", "elapsed_s": 0,
        "last_tool": "resume_actuator_dispatch",
        "work_units": max(1, _beats),
        "driver": SEAT,
    })

    def _no_advance(reason: str, **extra) -> dict:
        rad.record_attempt(scores_db, task_id)
        return {"ok": False, "task_id": task_id, "reason": reason, **extra}

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
            # sweep if it really does go quiet.
            return {"ok": False, "task_id": task_id, "step": job["step"],
                    "deferred": True,
                    "reason": f"already driving: held by {holder}"}

    from prism_service.inference import claude_cli
    try:
        result = claude_cli.invoke(
            job["instructions"], work_dir=work_dir, plugin_dir=work_dir,
            max_turns=_max_turns(), max_budget_usd=_max_budget_usd(),
            allowed_tools=BUILD_TOOLS, project=project,
            purpose=f"resume-actuator@{job['step']}#{task_id[:8]}")
    except Exception as exc:
        if claim is not None:
            claim.release(claim_id)
        return _no_advance(f"claude_cli invocation failed: {exc}",
                           step=job["step"])

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
        rad.record_attempt(scores_db, task_id)

    return {"ok": bool(report.get("ok")), "task_id": task_id,
            "step": step_id, "run_id": getattr(result, "run_id", None),
            "report": report}


def sweep_once_for(project: str) -> Optional[dict]:
    """One pass over `project`: continue an open retry (bypassing the
    'stalled' recheck -- see `_open_retry_task_id`), park it if its budget
    is spent, or else dispatch one newly-stalled task. Returns None when
    nothing was eligible."""
    from prism_service.services import resume_attempts_data as rad

    scores_db = _scores_db_for(project)
    max_retries = _max_retries()

    task_id = _open_retry_task_id(project)
    if task_id is not None:
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
