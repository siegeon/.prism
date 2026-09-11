"""Shared dispatch guard (task ab9166d5 incident, 2026-09-10).

Measured live on the AOS daemon: task ab9166d5 was parked `blocked` by
resume_actuator's own ceiling ("12 dispatches ... at the ceiling of 12"),
yet fresh `claude -p` children kept being spawned for it minutes apart --
GPU pinned 98%/278W for 6+ hours. The daemon log showed why:
resume_actuator and task_runner are TWO INDEPENDENT dispatchers, each with
its own claim-then-invoke path, and only resume_actuator counted its own
attempts (including ones it later DEFERRED because task_runner already
held the claim -- counted before the claim check, so a no-op still spent
budget). task_runner's own dispatches, the ones that actually ran the GPU
for up to 1800s each, were never counted by anyone, and nothing re-checked
task status at the one moment that matters: immediately before the
expensive call.

This module is THE one chokepoint every real `claude_cli.invoke()` call
must pass through, for every seat. `try_begin` re-reads the task FRESH
(never trusts an earlier eligibility check, which can go stale between
being read and being acted on) and is the single place that counts a
REAL dispatch -- so the ceiling it enforces cannot disagree with what
actually ran, the way resume_actuator's own count could. It is additive:
task_runner and resume_actuator keep their own claim leases, retry
budgets and eligibility checks exactly as they are; this is one more
gate, the last one, right before the GPU spends anything.

Two more pieces close the rest of the incident:
- `sweep_reap()` finds any `claude -p` process this daemon spawned (by
  ppid) whose task has since left a driving state, and kills it --
  the reaper for a child that was legitimately dispatched before a park
  landed mid-flight, which no per-call ceiling can prevent by itself.
- `DispatchTicket` re-beats `drive_heartbeat` every HEARTBEAT_INTERVAL_S
  while a real invoke is in flight. Neither of PRISM's two existing
  heartbeat producers (the implement.js prompt-text discipline, and
  drive_activity_observer's transcript-growth watch) can see this path:
  both read an on-disk session transcript, and every internal invoke here
  runs with --no-session-persistence and writes no such file. Without
  this, a genuinely running internal step goes heartbeat-stale after
  HEARTBEAT_WINDOW_S (180s) and activity_for renders it stalled --
  which is precisely what hid this incident from the Workflows view.

Every decision this module makes (refuse, park, reap) is written to the
task's own history via task_svc.record_history -- the SAME durable,
already-visible Trace surface every other seat's actions use. No new UI
surface. NEVER decides a gate.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

from prism_service.services import drive_heartbeat

SEAT = "prism-dispatch-guard"

# History-row actions this module writes. Distinct from resume_actuator's
# own DISPATCH_ACTION/PARKED_ACTION/RELEASED_ACTION names on purpose: this
# is a second, independent seat's bookkeeping, not a replacement for
# resume_actuator's own (which stays as-is, a redundant safety net for its
# own dispatches). dispatch_guard's ceiling is the one that actually
# governs every seat, because it is checked at the true chokepoint.
DISPATCH_ACTION = "dispatch_guard_dispatch"
REFUSED_ACTION = "dispatch_guard_refused"
PARKED_ACTION = "dispatch_guard_parked"
REAPED_ACTION = "dispatch_guard_reaped"
RELEASED_ACTION = "dispatch_guard_released"

# A task is only ever dispatched from this state. Fail closed: anything
# else (blocked, done, cancelled, pending, "") is refused. Checked FRESH,
# immediately before the call it guards -- never the eligibility read an
# earlier sweep made, which can go stale between being read and acted on.
DRIVING_STATUS = "in_progress"

# Statuses the reaper is authorized to act on. Deliberately NOT the
# complement of DRIVING_STATUS -- "pending" has no business having a live
# child either, but the incident's own authorization ("if someone is
# stopped, then it should stop") named exactly these three, and a reaper
# is destructive enough to stay literal about its mandate.
REAP_STATUSES = {"blocked", "done", "cancelled"}

DEFAULT_CEILING = 12
HEARTBEAT_INTERVAL_S = 60

# A task parked with a blocked_reason carrying one of these prefixes was
# stopped BY A GOVERNANCE SEAT, not by a person clicking block or by an
# ordinary dependency wait. `flow_start`'s own `_mark_in_progress` (both
# copies: api/conductor_flow.py and mcp/tools.py) used to flip ANY
# pending/blocked task straight back to in_progress the instant anything
# called conductor_work/flow_start for it -- silently un-parking a task
# this module or resume_actuator had just stopped, before try_begin's own
# status check ever ran. `is_governance_park` is the shared predicate both
# call sites use to refuse that resurrection instead; only an explicit
# `release()` (this module's or resume_actuator's) may lift one of these.
GOVERNANCE_PARK_PREFIXES = ("resume-actuator:", "dispatch-guard:")


def is_governance_park(blocked_reason) -> bool:
    reason = str(blocked_reason or "")
    return any(reason.startswith(p) for p in GOVERNANCE_PARK_PREFIXES)


def _log(msg: str) -> None:
    print(f"[dispatch-guard] {msg}", file=sys.stderr, flush=True)


def _ceiling() -> int:
    raw = os.environ.get("PRISM_DISPATCH_CEILING", "")
    try:
        return max(1, int(raw)) if raw.strip() else DEFAULT_CEILING
    except ValueError:
        return DEFAULT_CEILING


def _scores_db_for(project: str) -> str:
    from prism_service.project_context import get_project

    return str(get_project(project)._data_dir / "scores.db")


def _total_dispatches(task_svc, task_id: str) -> int:
    """REAL dispatches this module has counted for `task_id`, since the
    last human release. Counted from durable history so it survives a
    daemon restart and cannot be inflated by a dispatch that never
    happened (a deferral, a claim loss) -- every DISPATCH_ACTION row here
    was written at the one place a real invoke was about to run."""
    try:
        rows = task_svc.history(task_id) or []
    except Exception:
        return 0
    start = 0
    for i, r in enumerate(rows):
        if str(getattr(r, "action", "") or "") == RELEASED_ACTION:
            start = i + 1
    return sum(1 for r in rows[start:]
               if str(getattr(r, "action", "") or "") == DISPATCH_ACTION)


def release(project: str, task_id: str, actor: str = "human") -> dict:
    """The human 'the cause is fixed, try again' signal (mirrors
    resume_actuator.release) -- the only place the ceiling count resets.
    Only lifts a park THIS module made (blocked_reason carries this
    module's own prefix); any other blocked_reason is left untouched."""
    from prism_service.project_context import get_project

    ctx = get_project(project)
    task = ctx.task_svc.get(task_id)
    if task is None:
        return {"ok": False, "task_id": task_id, "reason": "no such task"}
    status = str(getattr(task, "status", "") or "")
    reason = str(getattr(task, "blocked_reason", "") or "")
    parked_by_guard = status == "blocked" and "dispatch-guard:" in reason
    if parked_by_guard:
        ctx.task_svc.update(task_id, status="in_progress", blocked_reason="")
    ctx.task_svc.record_history(
        task_id, action=RELEASED_ACTION,
        details=(f"released by {actor}; "
                 + ("unparked to in_progress" if parked_by_guard
                    else f"status left as {status or 'unknown'}")),
        actor=actor)
    return {"ok": True, "task_id": task_id, "unparked": parked_by_guard}


class DispatchTicket:
    """One real dispatch in flight. Re-beats drive_heartbeat every
    HEARTBEAT_INTERVAL_S for as long as it is open, so a step that runs
    past the 180s heartbeat window still reads 'driving' on the Workflows
    canvas instead of going stale mid-step -- the exact gap that hid this
    incident. work_units is elapsed seconds, which is strictly increasing
    for the life of one ticket (drive_heartbeat's monotonic guard rejects
    a repeated counter)."""

    def __init__(self, project: str, task_id: str, step: str, seat: str,
                 scores_db: str):
        self.project = project
        self.task_id = task_id
        self.step = step
        self.seat = seat
        self.scores_db = scores_db
        self.started_at = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _beat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            elapsed = int(time.monotonic() - self.started_at)
            try:
                drive_heartbeat.record_heartbeat(self.scores_db, {
                    "task_id": self.task_id, "step": self.step or "unknown",
                    "elapsed_s": elapsed,
                    "last_tool": "dispatch_guard_live",
                    "work_units": max(1, elapsed),
                    "driver": self.seat,
                })
            except Exception:
                pass

    def start(self) -> "DispatchTicket":
        self._thread = threading.Thread(
            target=self._beat_loop, name="prism-dispatch-heartbeat",
            daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at


def try_begin(project: str, task_id: str, step_id: str,
              seat: str) -> tuple[Optional[DispatchTicket], Optional[str]]:
    """THE chokepoint. Call this immediately before every real
    `claude_cli.invoke()`, after any claim/lease is already held. Returns
    `(ticket, None)` when the caller may proceed, or `(None, reason)`
    when it must not -- the caller is responsible for releasing its own
    claim and returning a refusal in that case; this function never
    touches another seat's claim."""
    from prism_service.project_context import get_project

    ctx = get_project(project)
    task_svc = ctx.task_svc
    task = task_svc.get(task_id)
    if task is None:
        return None, "unknown task"

    status = str(getattr(task, "status", "") or "")
    if status != DRIVING_STATUS:
        reason = (f"dispatch-guard: refused -- task status is {status!r}, "
                  f"not {DRIVING_STATUS!r}. A task that is not in a "
                  "driving state is never dispatched, however it got "
                  f"here (seat={seat}, step={step_id}).")
        try:
            task_svc.record_history(task_id, action=REFUSED_ACTION,
                                    details=reason, actor=seat)
        except Exception:
            pass
        return None, reason

    total = _total_dispatches(task_svc, task_id)
    ceiling = _ceiling()
    if total >= ceiling:
        reason = (f"dispatch-guard: {total} dispatches for this task, at "
                  f"the ceiling of {ceiling}. Refusing to dispatch again "
                  "-- parked for a person.")
        try:
            task_svc.update(task_id, status="blocked", blocked_reason=reason)
            task_svc.record_history(task_id, action=PARKED_ACTION,
                                    details=reason, actor=seat)
        except Exception:
            pass
        return None, reason

    try:
        task_svc.record_history(
            task_id, action=DISPATCH_ACTION,
            details=f"seat={seat}; step={step_id}; dispatch {total + 1}/{ceiling}",
            actor=seat)
    except Exception:
        pass

    scores_db = _scores_db_for(project)
    ticket = DispatchTicket(project, task_id, step_id, seat, scores_db)
    ticket.start()
    return ticket, None


def end_dispatch(ticket: Optional[DispatchTicket]) -> None:
    """Close a ticket opened by `try_begin`. Always safe to call, even
    with None (a refused dispatch never opened one)."""
    if ticket is not None:
        ticket.stop()


# ---------------------------------------------------------------------------
# The reaper: kills a `claude -p` child this daemon spawned for a task that
# has since left a driving state. Discovers pids by process ancestry
# (ppid == this daemon) and cwd (every task workspace is
# ~/.prism/task_workspaces/<task_id>), rather than by threading a pid back
# out of claude_cli.invoke (a blocking subprocess.run call today) -- this
# keeps the reaper fully decoupled from the hot invoke path, at the cost of
# only working on a real /proc + ps environment (true for every place this
# daemon runs: WSL dev and the AOS host alike).
# ---------------------------------------------------------------------------


def _find_daemon_children() -> list[dict]:
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid,args"],
            capture_output=True, text=True, timeout=10)
    except Exception as exc:
        _log(f"process scan failed: {exc}")
        return []
    if out.returncode != 0:
        return []
    my_pid = os.getpid()
    rows: list[dict] = []
    for line in out.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, ppid_s, args = parts
        if not args.startswith("claude -p") and not args.startswith("claude  -p"):
            continue
        try:
            pid, ppid = int(pid_s), int(ppid_s)
        except ValueError:
            continue
        if ppid != my_pid:
            continue
        rows.append({"pid": pid, "args": args})
    return rows


def _task_id_from_pid(pid: int) -> Optional[str]:
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None
    parts = [p for p in cwd.split("/") if p]
    if "task_workspaces" not in parts:
        return None
    idx = parts.index("task_workspaces")
    if idx + 1 >= len(parts):
        return None
    return parts[idx + 1]


def _proc_elapsed_s(pid: int) -> Optional[float]:
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etimes="],
            capture_output=True, text=True, timeout=5)
        raw = out.stdout.strip()
        return float(raw) if raw else None
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True  # fail closed: assume alive if we genuinely can't tell


def _terminate(pid: int, grace_s: float = 10.0) -> str:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already gone"
    except Exception as exc:
        return f"SIGTERM failed: {exc}"
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return "SIGTERM"
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
        return "SIGKILL (did not exit within grace)"
    except ProcessLookupError:
        return "SIGTERM (raced)"
    except Exception as exc:
        return f"SIGKILL failed: {exc}"


def _task_lookup(task_id: str):
    from prism_service.project_context import get_all_projects, get_project

    for pid in get_all_projects():
        try:
            task = get_project(pid).task_svc.get(task_id)
        except Exception:
            task = None
        if task is not None:
            return pid, task
    return None, None


def sweep_reap() -> list[dict]:
    """One reaper pass. Kills every daemon-spawned `claude -p` child whose
    task is no longer in REAP_STATUSES -- never touches a task that is
    `in_progress` (or anything else outside that explicit set), and never
    touches a process this daemon did not spawn. Returns what it did, for
    logging/tests; best-effort throughout, a lookup failure just skips
    that one pid rather than raising."""
    results: list[dict] = []
    for row in _find_daemon_children():
        pid = row["pid"]
        task_id = _task_id_from_pid(pid)
        if not task_id:
            continue
        project, task = _task_lookup(task_id)
        if task is None:
            continue
        status = str(getattr(task, "status", "") or "")
        if status not in REAP_STATUSES:
            continue
        elapsed = _proc_elapsed_s(pid)
        how = _terminate(pid)
        elapsed_txt = f"{elapsed:.0f}s" if elapsed is not None else "unknown"
        reason = (f"dispatch-guard: reaped pid {pid} ({elapsed_txt} "
                  f"runtime) via {how} -- task status is {status!r}, not "
                  "driving.")
        try:
            from prism_service.project_context import get_project

            get_project(project).task_svc.record_history(
                task_id, action=REAPED_ACTION, details=reason, actor=SEAT)
        except Exception:
            pass
        _log(reason)
        results.append({"pid": pid, "task_id": task_id, "project": project,
                        "status": status, "elapsed_s": elapsed, "how": how})
    return results


def _interval_s() -> int:
    raw = os.environ.get("PRISM_DISPATCH_REAPER_INTERVAL", "")
    try:
        return int(raw) if raw.strip() else 0
    except ValueError:
        return 0


def is_enabled() -> bool:
    return _interval_s() > 0


def _loop(interval_s: int,
          stop_event: Optional[threading.Event] = None) -> None:
    _log(f"started; interval={interval_s}s")
    while stop_event is None or not stop_event.is_set():
        try:
            sweep_reap()
        except Exception as exc:
            _log(f"sweep error: {exc}")
        if stop_event is not None:
            if stop_event.wait(interval_s):
                break
        else:
            time.sleep(interval_s)


def start_dispatch_reaper() -> Optional[threading.Thread]:
    """Spawn the reaper daemon thread, unless disabled via
    PRISM_DISPATCH_REAPER_INTERVAL<=0/unset (the default). Mirrors
    task_runner.start_task_runner / resume_actuator.start_resume_actuator."""
    interval = _interval_s()
    if interval <= 0:
        _log("disabled (default OFF; set PRISM_DISPATCH_REAPER_INTERVAL="
             "<seconds> to opt this environment in)")
        return None
    t = threading.Thread(target=_loop, args=(interval,),
                         name="prism-dispatch-reaper", daemon=True)
    t.start()
    return t
