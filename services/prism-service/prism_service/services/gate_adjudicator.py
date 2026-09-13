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
from datetime import datetime, timezone
from typing import Optional

from prism_service.services import system_activity

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
                    # task 594f9a58: the SAME string the certainty seat
                    # parks with, so the surfacer and the seat never
                    # disagree about why a human is being asked.
                    return dp.root_plan_gate_escalation_reason(
                        project, getattr(task, "id", ""), task, status)
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


def _task_field(t: object, name: str) -> str:
    val = t.get(name) if isinstance(t, dict) else getattr(t, name, "")
    return str(val or "")


def _backoff_key(tid: str, t: object) -> tuple:
    """Everything one gate decision for `t` depends on this sweep --
    task 2026-09-13 (adjmemo): updated_at alone missed a workspace whose
    tree moved without a matching row write, so a stale receipt kept
    re-refusing on a decision that had actually gone stale in the other
    direction. See gate_adjudicator_memo for the workspace-HEAD half."""
    from prism_service.services import gate_adjudicator_memo as _memo
    return _memo.adjudication_key(
        tid, _task_updated_at(t), _task_field(t, "gate_state"),
        _task_field(t, "workflow_step"))


def _backoff_should_skip(tid: str, t: object) -> bool:
    """True when this task refused recently and nothing about it changed."""
    st = _BACKOFF.get(tid)
    if st is None:
        return False
    prev_key, next_at, _delay = st
    if _backoff_key(tid, t) != prev_key:
        _BACKOFF.pop(tid, None)      # something moved -- re-attempt now
        return False
    return time.monotonic() < next_at


def _backoff_note_refused(tid: str, t: object) -> None:
    prev = _BACKOFF.get(tid)
    delay = min((prev[2] * 2.0) if prev else 60.0, _BACKOFF_CAP_S)
    _BACKOFF[tid] = (_backoff_key(tid, t), time.monotonic() + delay, delay)


def _backoff_clear(tid: str) -> None:
    _BACKOFF.pop(tid, None)


#: set by the most recent `sweep_once()` -- how many gate-step tasks were
#: actually eligible for adjudication this pass (pending/failed on a gate
#: step this seat handles), REGARDLESS of whether any were approved or
#: skipped by backoff. `_loop` reads this right after calling `sweep_once`
#: to decide whether the next wait should use the fast (pending-gate)
#: cadence or fall back to the long idle ceiling -- a module global rather
#: than a return-shape change so nothing that already calls `sweep_once()`
#: for its `approved` list needs to change.
_last_eligible_count = 0

#: set alongside `_last_eligible_count` -- how many of this pass's eligible
#: tasks actually had a changed adjudication key (a real rubric/oracle/git
#: attempt), and how many of THOSE were decided. `_loop` reads these to
#: mark a pass "active" only when real work happened, not merely because a
#: backlog of unchanged pending gates still exists (task adjmemo), and to
#: report "N at gates, M changed, K decided" on the activity feed
#: (tick-cost pass, owner brief 2026-09-13).
_last_changed_count = 0
_last_decided_count = 0


# PROJECT-LEVEL SKIP (2026-09-13, task adjmemo). The per-task backoff above
# only ever saved the cost of ONE task's adjudication -- every sweep still
# fetched and reconstructed EVERY task row in EVERY project (946 rows) just
# to re-discover the same ~26 gate candidates. If nothing has moved for a
# project since this seat last looked at it -- no `task_changed`/`shipped`
# wakeup -- skip the fetch entirely and reuse last pass's eligible count for
# the idle-vs-fast cadence decision in `_loop`. Owner 2026-09-13 ("it's all
# reactive and real time"): there is no default wall-clock safety net any
# more -- a write that bypasses task_service.update()/ship_worker's own
# signal calls (a direct DB poke) is invisible to this skip by design,
# same as every other reactive worker in this file's family. An operator
# who genuinely needs a periodic re-scan for such an environment opts in
# explicitly with PRISM_WORKER_FALLBACK_S (unset by default).
_LAST_PROJECT_SCAN: dict[str, float] = {}
_LAST_PROJECT_ELIGIBLE: dict[str, int] = {}

# Wall-clock ceiling on the EXPENSIVE half of one sweep_once() pass (actual
# rubric/oracle/git adjudication of a task whose key changed) -- owner
# 2026-09-13: no background pass may hold the GIL long enough to delay a
# live request. A task that does not fit the budget is simply left for the
# next tick; its memo is never written, so it is retried, never dropped.
_SWEEP_BUDGET_S = 2.0

# Ceiling on back-to-back FORCED passes `_loop` runs right after warmup to
# drain a real boot-time backlog (task a65c66e5, third round) -- a backstop
# against a pathological signal loop, never expected to bind in practice
# (31 real pending gates drained in a handful of passes live); if it ever
# does, the loop falls through to normal reactive waiting rather than
# holding up startup forever.
_MAX_BOOT_DRAIN_PASSES = 25


def _project_needs_scan(pid: str, wakeups_mod) -> bool:
    last = _LAST_PROJECT_SCAN.get(pid)
    if last is None:
        return True
    # PRISM_WORKER_FALLBACK_S, unset by default -- see
    # wakeups.worker_fallback_s, same contract, same env var, shared
    # across every worker in this reactive family (no per-file duplicate).
    fallback = wakeups_mod.worker_fallback_s()
    if fallback is not None and time.time() - last >= fallback:
        return True
    # wait(..., timeout=0) rather than last_signal_at(): the worker-host
    # process split (task workerproc) moved this seat into a SEPARATE OS
    # process from the API, and last_signal_at() only ever reads THIS
    # process's in-memory _LAST dict -- it would never see a task_changed
    # signal task_service.update() raised in the API process. wait()'s
    # own _cross_has_new() check does consult the cross-process wakeups.db
    # bridge, and timeout=0 makes it a non-blocking single check rather
    # than an actual wait.
    return wakeups_mod.wait(("task_changed", "shipped", "deployed"),
                            project=pid, timeout=0, since=last)


def sweep_once(force: bool = False, force_backoff: Optional[bool] = None
              ) -> list[dict]:
    """One pass over every project: adjudicate each PENDING green_gate.
    Returns the list of approvals made (empty when nothing was decidable).

    `force=True` bypasses the project-level scan skip (`_project_needs_
    scan`) for this one pass -- task a65c66e5, 2026-09-13: a `deployed`
    signal means the CODE that reads a parked row may have just changed
    (the certainty seat's own self-heal), not the row itself, so the
    project memo -- keyed on the row being unchanged -- would never
    notice on its own.

    `force_backoff` (defaults to the SAME value as `force`, so an existing
    `force=True` caller keeps bypassing both memos exactly as before)
    separately controls the per-task backoff (`_backoff_should_skip`).
    Pass `force=True, force_backoff=False` explicitly for a DRAIN
    CONTINUATION pass (task a65c66e5, third round): `_loop` keeps forcing
    the project scan across several back-to-back passes to work through a
    boot-time backlog bigger than one `_SWEEP_BUDGET_S` window, but a task
    already refused earlier THIS SAME boot must still respect its fresh
    backoff delay -- bypassing it too would let the seat spend every
    pass's budget re-attempting the same already-refused, front-of-list
    tasks forever, starving the never-yet-touched ones (like a65c66e5
    itself, live) further back in the same backlog."""
    if force_backoff is None:
        force_backoff = force
    global _last_eligible_count, _last_changed_count, _last_decided_count
    from prism_service.project_context import get_all_projects, get_project
    from prism_service.services import wakeups
    approved: list[dict] = []
    eligible_count = 0
    changed_count = 0
    decided_count = 0
    started = time.monotonic()
    deadline = started + _SWEEP_BUDGET_S
    for pid in get_all_projects():
        if not force and not _project_needs_scan(pid, wakeups):
            eligible_count += _LAST_PROJECT_ELIGIBLE.get(pid, 0)
            continue
        try:
            ctx = get_project(pid)
            svc = ctx.conductor_svc
            # LEAN SNAPSHOT (tick-cost pass, owner brief 2026-09-13): a
            # project's tasks outside the gate steps below (the vast
            # majority once it has run a while) never pay for a full
            # `list()` row conversion just to be filtered back out a line
            # later -- see TaskService.gate_sweep_rows's own docstring.
            tasks = ctx.task_svc.gate_sweep_rows()
        except Exception as exc:
            _log(f"{pid}: project unavailable ({exc})")
            continue
        project_eligible = 0
        for t in tasks:
            step = t.get("workflow_step") if isinstance(t, dict) \
                else getattr(t, "workflow_step", "")
            gate = t.get("gate_state") if isinstance(t, dict) \
                else getattr(t, "gate_state", "")
            tid = t.get("id") if isinstance(t, dict) else getattr(t, "id", "")
            if not tid:
                continue

            # TERMINAL STEP CLOSURE (task 23019de9): close tasks that reach
            # a step with type=done and have no outstanding gate. This check
            # runs BEFORE the gate-step filter, so a task at "done" gets a
            # close attempt before we skip it for not being in the gate list.
            # Use backoff so a task that cannot close does not spam history.
            if step == "done":
                gate_state = gate or ""
                if gate_state not in ("pending", "failed"):
                    if not _backoff_should_skip(tid, t):
                        try:
                            from prism_service.api.conductor_flow import _close_if_terminal
                            _close_if_terminal(svc, tid)
                            _backoff_clear(tid)
                        except Exception as exc:
                            _log(f"{pid}/{tid[:8]}: terminal close raised ({exc})")
                            _backoff_note_refused(tid, t)
                continue

            if step not in ("green_gate", "red_gate",
                            "story_gate", "plan_gate",
                            "decide", "review"):
                continue
            # green_gate also sweeps 'failed' — adjudicate_green_gate
            # re-presents ONLY machine refusal artifacts, never a human
            # reject (it checks the history itself). decide (triage workflow)
            # has the same guard: re-sweep ONLY machine/config refusals, never
            # human rejects. Every other gate: pending only — a decided gate
            # stays decided.
            if step == "green_gate":
                if gate not in ("pending", "failed"):
                    continue
            elif step == "decide":
                if gate == "pending":
                    pass
                elif gate == "failed":
                    # Re-sweep a FAILED decide gate ONLY when the refusal
                    # came from the machine (configuration/validation error),
                    # never when a human explicitly rejected it. Check the
                    # gate_decide history: action=reject means human decision.
                    if not svc._failed_gate_is_refused_approve(tid, "decide"):
                        continue
                else:
                    continue
            elif gate != "pending":
                continue
            # Reached here: a real gate-step task this seat is responsible
            # for this pass, whether or not backoff ends up skipping it --
            # this is what "a gate is genuinely parked" means for the
            # loop's fast-vs-idle cadence decision below.
            eligible_count += 1
            project_eligible += 1
            if not force_backoff and _backoff_should_skip(tid, t):
                continue
            if time.monotonic() > deadline:
                # Over the per-sweep time budget (owner 2026-09-13: no
                # background pass may hold the GIL long enough to delay a
                # live request). Leave this task's memo untouched so the
                # NEXT tick retries it -- deferred, never dropped.
                continue
            changed_count += 1
            try:
                if step == "decide":
                    # The triage workflow's ONLY gate. It carries no rubric
                    # (validation=None), so the seat scores the CLASSIFICATION
                    # the classify step produced and refuses a missing,
                    # bucket-less or unreasoned one. A refusal leaves the gate
                    # PENDING with a stamped reason, never 'failed'.
                    from prism_service.services import triage_decision
                    res = triage_decision.adjudicate(svc, ctx.task_svc, tid)
                elif step == "review":
                    # The promote_to_law workflow's ONLY gate. It carries no
                    # rubric (validation=None), so the seat scores the draft
                    # output from the draft step, checking for valid TTL and
                    # proper structure. A refusal leaves the gate PENDING with
                    # a stamped reason so the draft can be revised.
                    from prism_service.services import promote_to_law_review
                    res = promote_to_law_review.adjudicate(svc, ctx.task_svc, tid)
                elif step == "green_gate":
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
                    _pt = None
                    if step == "plan_gate":
                        from prism_service.services import plan_gate_checks as _pgc
                        _pt = ctx.task_svc.get(tid)
                        _hold = _pgc.refusal(_pt, pid) if _pt is not None else ""
                    if _hold:
                        res = None
                    elif step == "plan_gate" and _pt is not None:
                        # task 594f9a58: the certainty seat decides a ROOT
                        # plan_gate first. None means "not my remit" (a
                        # child task, or a rubric not yet verified), which
                        # falls through to the unchanged rubric autoclear.
                        from prism_service.services import design_packet as _dp
                        outcome = _dp.adjudicate_root_plan_gate(
                            svc, tid, _pt, pid)
                        if outcome is None:
                            res = svc.adjudicate_rubric_gate(tid)
                        else:
                            res = outcome if outcome.get("ok") else None
                    else:
                        res = svc.adjudicate_rubric_gate(tid)
            except Exception as exc:
                _log(f"{pid}/{tid[:8]}: adjudication raised ({exc})")
                continue
            if res and res.get("ok"):
                decided_count += 1
                _backoff_clear(tid)
                # RECORD THE CONCLUDED GATE (task 8fbd5cf0). This seat never
                # passes through conductor_flow.flow_report, so without this
                # the canvas has no record of a machine-decided gate at all.
                try:
                    from prism_service.services.flow_run_recorder import (
                        record_node_execution)
                    record_node_execution(
                        str(ctx._data_dir / "scores.db"),
                        {"task_id": tid, "node_id": step,
                         "actor": "conductor-adjudicator",
                         "workflow_id": "conductor", "outcome": "pass",
                         "reason": str(res.get("reason") or
                                       "approved on machine evidence")[:500],
                         # TRUE WALL TIME: t's own updated_at is when the
                         # task last moved onto this step; never a clock
                         # read invented here.
                         "started_at": _task_updated_at(t),
                         "ended_at": datetime.now(timezone.utc).isoformat()},
                        project=pid)
                except Exception:
                    pass
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
                    # task fb997b1d: a REFUSED plan/story rubric rewinds to
                    # its producing agent step instead of parking for ever.
                    # Without this the row is unreachable: the drive seat
                    # skips gate steps, the adjudicator withholds a refused
                    # rubric, and nothing else moves it -- so only a person
                    # hand-editing plan_doc could free it.
                    # red_gate joins them (task 1bcb2b24): it is the one
                    # gate a human must never be asked to clear, so without
                    # a rewind a refused red is unreachable from both sides.
                    if task is not None and step in ("plan_gate",
                                                     "story_gate",
                                                     "red_gate"):
                        from prism_service.services import plan_rewind
                        pw = plan_rewind.maybe_rewind(ctx, task, pid)
                        if pw and pw.get("ok"):
                            _log(f"{pid}/{tid[:8]}: {step} rubric rewind "
                                 f"-> {pw.get('to_step')}")
                            continue
                    if task is not None:
                        _decline = _pending_decline_reason(
                            svc, task, step, pid)
                        _write_pending_reason(ctx, task, _decline)
                        # RUBRIC FIRST, INFERENCE FOR THE RESIDUE (owner
                        # 2026-08-29: "it should be inferred AND rubric ...
                        # make it the MOST deterministic it can be by
                        # codifying things as much as it can, and leaving
                        # room for inference to deal with unknowns").
                        #
                        # A codified REFUSAL is final for this pass: it is
                        # already a decision, stated in words, and inference
                        # does not get to talk it away. Only when the
                        # deterministic half has nothing left to say -- no
                        # refusal, yet still no approval -- is what remains
                        # judgement a rubric cannot express, and only then
                        # does a real agent seat read the packet.
                        if not str(_decline or "").strip():
                            from prism_service.services import gate_agent
                            _inf = gate_agent.adjudicate(pid, tid, step)
                            if _inf:
                                _log(f"{pid}/{tid[:8]}: {step} decided by "
                                     f"the inference seat")
                    # Record the refusal AFTER the write: _write_pending_reason
                    # may move updated_at, and the backoff compares against the
                    # value this pass leaves behind.
                    _backoff_note_refused(tid, ctx.task_svc.get(tid) or task)
                except Exception as exc:
                    _log(f"{pid}/{tid[:8]}: reason-surface skipped ({exc})")
        _LAST_PROJECT_SCAN[pid] = time.time()
        _LAST_PROJECT_ELIGIBLE[pid] = project_eligible
    _last_eligible_count = eligible_count
    _last_changed_count = changed_count
    _last_decided_count = decided_count
    elapsed_ms = (time.monotonic() - started) * 1000.0
    _log(f"{eligible_count} at gates · {changed_count} changed · "
         f"{decided_count} decided · {elapsed_ms:.0f} ms")
    return approved


def _loop(interval_s: int) -> None:
    from prism_service.services import wakeups

    _log(f"started; interval={interval_s}s (gate signal only unless "
         "PRISM_WORKER_FALLBACK_S is set)")
    wakeups.lower_thread_priority()
    wakeups.wait_out_startup_warmup()
    baseline = time.time()
    first_pass = True
    boot_drain_passes = 0
    while True:
        # THE FIRST PASS(ES) AFTER WARMUP ARE UNCONDITIONAL (task a65c66e5,
        # 2026-09-13, second and third rounds): relying on a `deployed`
        # signal to force a re-sweep only works for an in-process restart
        # that lands AFTER this loop is already waiting -- main.py's own
        # boot signal fires at API startup, before the worker-host process
        # (and this loop) even exists, so a fresh process never sees it and
        # a fix that only landed in a new deploy would sit unswept until
        # the NEXT deploy.
        #
        # A `deployed` signal since the last wait() (a LATER in-process
        # restart is not possible for this thread, but a landing that
        # confirmed via deploy_worker mid-run is) means the CODE that
        # reads a parked gate may have just changed, not the row itself --
        # force one more full pass, bypassing both the project-level scan
        # skip and the per-task backoff, so that park is re-evaluated too.
        deployed_since = bool(
            wakeups.changed_since(["deployed"], None, baseline))
        force = first_pass or deployed_since
        # Bypass the per-task backoff too on the VERY FIRST pass (nothing
        # has a backoff entry yet) and on a fresh `deployed` signal (a
        # landing may make an already-backed-off task decidable again) --
        # but NOT on a boot-drain CONTINUATION pass (boot_drain_passes >
        # 0 below): real backoff must keep gating whatever this same boot
        # already refused, so the budget goes to never-yet-touched tasks
        # instead (see sweep_once's own docstring).
        force_backoff = deployed_since or boot_drain_passes == 0
        try:
            with system_activity.pass_("gate_adjudicator", "*", "sweep_once") as info:
                approved = sweep_once(force=force, force_backoff=force_backoff)
                # "active" means real work happened -- a changed key was
                # actually adjudicated, or something was decided. A
                # backlog of unchanged pending gates (the common case once
                # the per-project/per-task memo is warm) must read QUIET,
                # never churn, on the Live page (task adjmemo).
                info["active"] = bool(approved) or _last_changed_count > 0
                # Tick-cost pass (owner brief, 2026-09-13): make the
                # backoff's own cost win legible on the activity feed --
                # a human should be able to tell "26 at gates, 0 changed,
                # 0 decided" (near-zero cost) apart from an actual sweep.
                info["detail"] = (f"{_last_eligible_count} at gates, "
                                  f"{_last_changed_count} changed, "
                                  f"{len(approved)} decided")
        except Exception as exc:
            _log(f"sweep error: {exc}")
            first_pass = False

        if first_pass:
            # DRAIN THE FULL BOOT BACKLOG BEFORE GOING REACTIVE. Live
            # 2026-09-13: sweep_once's own per-pass time budget
            # (_SWEEP_BUDGET_S, 2s -- no background pass may hold the GIL
            # that long) left MOST of a real 31-gate backlog merely
            # DEFERRED after just one forced pass (10 changed, then the
            # loop would have gone reactive) -- and once reactive, a
            # deferred task's own eventual write happens BEFORE the
            # post-sweep wait() baseline below, so it can never
            # self-trigger the very re-look it still needs; it sits stuck
            # until some UNRELATED external signal happens to arrive.
            # `eligible_count > changed_count` on a FORCED pass (backoff
            # is bypassed, so the only reason something is not "changed"
            # is the time budget) means real work is still waiting -- so
            # force another pass immediately, no wait() in between, each
            # still capped at _SWEEP_BUDGET_S so the GIL is never held
            # longer in any single call. A single-gate boot (the shape
            # this seat's own tests pin) drains in exactly one pass, since
            # nothing there ever hits the budget.
            boot_drain_passes += 1
            still_draining = (_last_eligible_count > _last_changed_count
                              and boot_drain_passes < _MAX_BOOT_DRAIN_PASSES)
            if still_draining:
                continue
            first_pass = False
        # Wake on "shipped" too -- a push landing (ship_worker) can free a
        # green_gate or resolve a workspace-freshness refusal exactly like
        # a task_changed row does; waiting on task_changed alone left the
        # fallback timeout as the only way such a change was ever noticed.
        # since= is deliberately OMITTED here (defaults to None -> baseline
        # = now, taken AFTER the sweep above, not before it). Task
        # b490fabc/host-tight-loop: a pre-sweep baseline (the old
        # `since=sweep_started`) sees this sweep's OWN adjudication
        # (approving a gate writes task_changed) as a "new" signal the
        # instant wait() is entered, self-retriggering forever with zero
        # external cause -- measured ~390 spurious passes/s in isolation,
        # and live 100% CPU with no signals on the box. A post-sweep
        # baseline still catches a signal from a DIFFERENT
        # process/request (the thing this reactive design exists for)
        # without re-firing on work this very sweep already did.
        # timeout=worker_fallback_s() is None by default -- no periodic
        # wake at all unless an operator explicitly opts in (owner
        # 2026-09-13: "it's all reactive and real time").
        baseline = time.time()
        wakeups.wait(["task_changed", "shipped", "deployed"],
                     timeout=wakeups.worker_fallback_s())


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
