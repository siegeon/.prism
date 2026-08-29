"""Green-gate machine adjudicator sweep — the conductor's own gate seat.

Sweeps every project's tasks parked PENDING at green_gate on a cadence and
lets ConductorService.adjudicate_green_gate decide each one: exercise the
oracle when it is machine-runnable and unevidenced for the current tree,
approve on a FRESH PASSING EvidenceReceipt as ``conductor-adjudicator``,
and leave everything else (manual-evidence oracles, epics, failed gates,
tried-and-failing evidence) for a human. Task 1d3322a6, owner directive
2026-07-15: customers cannot click our board — a gate whose oracle the
server can run itself must clear itself; the human stays in the loop via
visibility, reject and override, not as a required click.

SHIPS OFF BY DEFAULT (owner decision 2026-07-15, AskUserQuestion "Ship it
OFF by default"): human clicks remain the norm; an environment opts in
with PRISM_GATE_ADJUDICATOR_INTERVAL=<seconds>. The flow-entry hook in
conductor_flow honors the same switch via is_enabled().

Mirrors the other lifespan workers (watchdog / understand_drainer): one
daemon thread, env-gated. Oracle runs happen on THIS thread (they may
take minutes), which is exactly why the seat is not a maintenance-clock
pass — a slow mint must never starve the shared clock.
"""
from __future__ import annotations

import os
import sys
import threading
import time

DEFAULT_INTERVAL_S = 0  # OFF unless an environment explicitly opts in


def _interval_s() -> int:
    raw = os.environ.get("PRISM_GATE_ADJUDICATOR_INTERVAL", "")
    try:
        return int(raw) if raw.strip() else DEFAULT_INTERVAL_S
    except ValueError:
        return DEFAULT_INTERVAL_S


def _log(msg: str) -> None:
    print(f"[gate-adjudicator] {msg}", file=sys.stderr, flush=True)


def is_enabled() -> bool:
    """True when this environment opted into the seat (interval > 0).
    Consulted by the conductor_flow entry hook too, so OFF means OFF —
    neither the sweep nor the flow-entry adjudication runs."""
    return _interval_s() > 0


def _pending_decline_reason(svc, task, step, project) -> str:
    """Why a PENDING gate could not auto-clear this sweep — for the driving
    agent (or the reviewing human) to self-diagnose (task 8f48f9bb). story/plan
    -> the rubric verdict's reason; red -> the latest non-passing
    EvidenceReceipt's reason; green -> the oracle receipt-refusal (e.g. a
    browser/manual_evidence_required oracle that no machine runner can pass, so
    it correctly awaits a human — surfaced so it reads as "why", not empty).
    Returns "" when there is no actionable reason. Best-effort — never raises
    into the sweep."""
    try:
        if step in ("story_gate", "plan_gate"):
            validation = svc._validation_for_gate(step)
            if not validation:
                return ""
            check = svc._verify_rubric_gate(task, validation)
            if check.get("verified") is not True:
                return str(check.get("reason") or "")
            if step == "plan_gate":
                # Deterministic plan teeth first (task 72ccaf94, five rounds
                # of hand-caught defects) -- same module, same verdict, as
                # api/conductor_flow.py's entry-time seat, so both seats
                # agree about why a plan_gate did not clear.
                from prism_service.services import plan_gate_checks as _pgc
                _cr = _pgc.refusal(task, project)
                if _cr:
                    return _cr
            if step == "plan_gate":
                # task c016667f, AC-5 third seat: a rubric-verified plan is
                # not enough — this used to `return ""` here (the exact
                # empty-reason class of failure e0149f1f closed), leaving a
                # driving agent with nothing to act on.
                from prism_service.services import design_packet as dp
                status = dp.approval_status(project, getattr(task, "id", ""),
                                            task)
                if not status.get("approved"):
                    return str(status.get("reason") or "")
            return ""
        if step == "red_gate":
            from prism_service.services import oracle_spec as osp
            rc = osp.latest_receipt(project, getattr(task, "id", "") or "")
            if rc is not None and not getattr(rc, "passed", False):
                return str(getattr(rc, "reason", "") or "")
        if step == "green_gate":
            refusal, _rc = svc._oracle_receipt_refusal(
                task, override=False, reason="")
            return str(refusal or "")
    except Exception:
        return ""
    return ""


def _write_pending_reason(ctx, task, reason) -> None:
    """Stamp a PENDING gate's decline reason onto task.gate_reason so a driving
    agent reads WHY it parked, instead of an empty string it can't act on.
    No-op unless the gate is pending and the reason is new; NEVER advances
    gate_state, NEVER re-stamps a gate already decided (passed/failed)."""
    if not reason:
        return
    if getattr(task, "gate_state", "") != "pending":
        return
    if (getattr(task, "gate_reason", "") or "") == reason:
        return
    try:
        ctx.task_svc.update(getattr(task, "id", "") or "", gate_reason=reason)
    except Exception:
        pass


# BACKOFF ON AN UNCHANGED REFUSAL (2026-08-29).
#
# The sweep re-attempted every pending gate every interval, forever, and on
# each refusal wrote a gate_decide row plus an updated row. Some of those
# gates the machine seat can NEVER decide: a demo/review proof_type is
# human-only by owner rule eaafdf75 and adjudicate_green_gate returns None
# for it BY DESIGN, so three tasks were each being re-attempted 1,440 times
# a day for a verdict that cannot exist. Measured on prism/tasks.db: ten
# parked tasks, ~19,000 history rows and ~10 MB a DAY, 110 MB total.
#
# A refusal that has not changed will not change on the next tick either, so
# the delay doubles per unchanged pass up to _BACKOFF_CAP_S. Anything that
# ACTUALLY changes about the task (a freshly minted receipt, a push, a human
# edit) moves its updated_at, which resets the backoff to the next sweep --
# so this delays only repetition, never a real decision.
#
# In-memory and per-process: a daemon restart re-attempts everything once,
# which is the correct bias.
_BACKOFF_CAP_S = 1800.0
_BACKOFF: dict[str, tuple[str, float, float]] = {}


def _task_updated_at(t: object) -> str:
    if isinstance(t, dict):
        return str(t.get("updated_at") or "")
    return str(getattr(t, "updated_at", "") or "")


def _backoff_should_skip(tid: str, t: object) -> bool:
    """True when this task refused recently and nothing about it changed."""
    st = _BACKOFF.get(tid)
    if st is None:
        return False
    prev_updated, next_at, _delay = st
    if _task_updated_at(t) != prev_updated:
        _BACKOFF.pop(tid, None)      # something moved -- re-attempt now
        return False
    return time.monotonic() < next_at


def _backoff_note_refused(tid: str, t: object) -> None:
    prev = _BACKOFF.get(tid)
    delay = min((prev[2] * 2.0) if prev else 60.0, _BACKOFF_CAP_S)
    _BACKOFF[tid] = (_task_updated_at(t), time.monotonic() + delay, delay)


def _backoff_clear(tid: str) -> None:
    _BACKOFF.pop(tid, None)


def sweep_once() -> list[dict]:
    """One pass over every project: adjudicate each PENDING green_gate.
    Returns the list of approvals made (empty when nothing was decidable)."""
    from prism_service.project_context import get_all_projects, get_project
    approved: list[dict] = []
    for pid in get_all_projects():
        try:
            ctx = get_project(pid)
            svc = ctx.conductor_svc
            tasks = ctx.task_svc.list()
        except Exception as exc:
            _log(f"{pid}: project unavailable ({exc})")
            continue
        for t in tasks:
            step = t.get("workflow_step") if isinstance(t, dict) \
                else getattr(t, "workflow_step", "")
            gate = t.get("gate_state") if isinstance(t, dict) \
                else getattr(t, "gate_state", "")
            tid = t.get("id") if isinstance(t, dict) else getattr(t, "id", "")
            if not tid or step not in ("green_gate", "red_gate",
                                       "story_gate", "plan_gate"):
                continue
            # green_gate also sweeps 'failed' — adjudicate_green_gate
            # re-presents ONLY machine refusal artifacts, never a human
            # reject (it checks the history itself). Every other gate:
            # pending only — a decided gate stays decided.
            if step == "green_gate":
                if gate not in ("pending", "failed"):
                    continue
            elif gate != "pending":
                continue
            if _backoff_should_skip(tid, t):
                continue
            try:
                if step == "green_gate":
                    res = svc.adjudicate_green_gate(tid)
                elif step == "red_gate":
                    # demo rubric first (task 59ddfcbc), then the
                    # test-proof red seat on a fresh RED receipt (task
                    # a5e8d877); each method refuses foreign shapes.
                    res = (svc.adjudicate_demo_red_gate(tid)
                           or svc.adjudicate_test_red_gate(tid))
                else:
                    # story/plan rubric RE-SWEEP (task a5e8d877 gap 2,
                    # strand mx-2812f9): the entry-time autoclear ran
                    # once; re-score now-compliant PENDING gates.
                    # A plan_gate with a deterministic-tooth refusal is
                    # withheld here rather than approved on the rubric
                    # alone; _pending_decline_reason then stamps the same
                    # refusal onto gate_reason for the driver to act on.
                    _hold = ""
                    if step == "plan_gate":
                        from prism_service.services import plan_gate_checks as _pgc
                        _pt = ctx.task_svc.get(tid)
                        _hold = _pgc.refusal(_pt, pid) if _pt is not None else ""
                    res = None if _hold else svc.adjudicate_rubric_gate(tid)
            except Exception as exc:
                _log(f"{pid}/{tid[:8]}: adjudication raised ({exc})")
                continue
            if res and res.get("ok"):
                _backoff_clear(tid)
                approved.append({"project": pid, "task_id": tid, **res})
                _log(f"{pid}/{tid[:8]}: {step} approved on machine "
                     f"evidence -> {res.get('to_step', 'advanced')}")
            else:
                # task 8f48f9bb — the gate did NOT auto-clear this pass. The
                # adjudicator already computed WHY (rubric verdict / red
                # receipt) and threw it away; surface it on task.gate_reason so
                # a driving agent self-diagnoses instead of stalling to ping a
                # human. Never advances gate_state (stays pending).
                try:
                    task = ctx.task_svc.get(tid)
                    # task ad92c0e9: a FRESH FAILED green receipt rewinds
                    # the drive to implement_tasks (or parks on a spent
                    # budget) instead of stamping a pending reason.
                    if task is not None and step == "green_gate":
                        from prism_service.services import green_rewind
                        rw = green_rewind.maybe_rewind(ctx, task, pid)
                        if rw:
                            _log(f"{pid}/{tid[:8]}: green_gate rewind "
                                 f"-> {rw.get('to_step') or 'parked'}")
                            continue
                    if task is not None:
                        _write_pending_reason(
                            ctx, task,
                            _pending_decline_reason(svc, task, step, pid))
                    # Record the refusal AFTER the write: _write_pending_reason
                    # may move updated_at, and the backoff compares against the
                    # value this pass leaves behind.
                    _backoff_note_refused(tid, ctx.task_svc.get(tid) or task)
                except Exception as exc:
                    _log(f"{pid}/{tid[:8]}: reason-surface skipped ({exc})")
    return approved


def _loop(interval_s: int) -> None:
    _log(f"started; interval={interval_s}s")
    while True:
        try:
            sweep_once()
        except Exception as exc:
            _log(f"sweep error: {exc}")
        time.sleep(interval_s)


def start_gate_adjudicator() -> threading.Thread | None:
    """Spawn the adjudicator daemon thread, unless disabled via
    PRISM_GATE_ADJUDICATOR_INTERVAL=0. Mirrors start_understand_drainer."""
    interval = _interval_s()
    if interval <= 0:
        _log("disabled (default OFF; set PRISM_GATE_ADJUDICATOR_INTERVAL="
             "<seconds> to opt this environment in)")
        return None
    t = threading.Thread(target=_loop, args=(interval,),
                         name="prism-gate-adjudicator", daemon=True)
    t.start()
    return t
