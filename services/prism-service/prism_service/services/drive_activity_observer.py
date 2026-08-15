"""Server-side, observed-activity heartbeat producer (task dd1e8871).

Sequel to e3b7ebf6/mx-1a134a/mx-b26121: drive_heartbeat.py's channel
exists, but until now its ONLY producer was PROMPT TEXT in
.claude/workflows/implement.js instructing a step agent to self-issue a
curl every 5-8 tool calls -- pure agent discipline, with nothing
backstopping a lapse. On 170991cc that discipline failed mid-step (beats
stopped at 01:47 while the session transcript kept growing to 655KB
through 02:01) and activity_for read adrift/stalled for 14+ minutes of
genuinely healthy work.

This module is a SECOND, server-side producer onto the SAME
drive_heartbeats store. Strictly ADDITIVE: drive_heartbeat.py and
conductor_service.py (a control_plane.POLICY_FILES entry) are both
read-only from here -- this module only ever CALLS their existing
public/private methods, and the prompt-text producer in implement.js is
left untouched as a second, independent path to the same beat.

THE CORE RULE (likely_misfire #1): a beat is emitted ONLY on OBSERVED
transcript growth (token count increasing tick over tick), NEVER on
elapsed wall-clock alone. A genuinely hung/looping agent's transcript
stops growing, so it receives no beat here and its heartbeat_age_s is
free to lapse past HEARTBEAT_WINDOW_S exactly as it would with zero
producers -- the dead-worker/claim-expiry signal this task's own
likely_misfire named survives untouched.

Scope: the MAIN step agent inside one long implement-workflow step. Does
NOT cover child-lane/fan-out heartbeats (task 774f11d1's separate scope).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Callable, Optional

from prism_service.services import drive_heartbeat


def _log(msg: str) -> None:
    print(f"[drive-activity-observer] {msg}", file=sys.stderr, flush=True)


# Default sweep cadence -- comfortably inside HEARTBEAT_WINDOW_S (180s) so
# a genuinely-driving task never has time to go stale between ticks.
DEFAULT_INTERVAL_S = 20

# In-process baseline: task_id -> last observed token reading. A single
# long-lived daemon thread (start_drive_activity_observer) owns this
# dict; no cross-process persistence needed (mirrors task_runner's own
# in-process _IN_FLIGHT guard).
_last_tokens: dict[str, int] = {}

# The running sweep thread, if any (module-level so a second start call
# is idempotent instead of stacking threads).
_thread: Optional[threading.Thread] = None


def _interval_s() -> int:
    raw = os.environ.get("PRISM_DRIVE_ACTIVITY_OBSERVER_INTERVAL", "")
    try:
        return int(raw) if raw.strip() else DEFAULT_INTERVAL_S
    except ValueError:
        return DEFAULT_INTERVAL_S


def is_enabled() -> bool:
    """Default ON -- this is a correctness backstop for the existing
    prompt-text heartbeat producer, not an opt-in feature (unlike
    gate_adjudicator/task_runner). PRISM_DRIVE_ACTIVITY_OBSERVER_INTERVAL=0
    opts an environment out, mirroring sqlite_maint's off switch."""
    return _interval_s() > 0


def _default_tokens_reader(task_id: str, session_id: str, cond) -> int:
    """Live transcript token count for `session_id`, via the SAME on-disk
    reader conductor_service already uses for session_quiet_s -- reused,
    never reimplemented (conductor_service.py stays read-only)."""
    from prism_service.services.claude_transcripts import (
        live_tokens_for_session,
    )
    try:
        project_path = cond._project_source_path()
        override_dir = cond._project_override_dir()
        return live_tokens_for_session(
            session_id, project_path, override_dir=override_dir)
    except Exception:
        logging.getLogger(__name__).debug(
            "drive_activity_observer: default tokens_reader failed for %s",
            task_id, exc_info=True)
        return 0


def _candidate_tasks(task_svc) -> list:
    """in_progress tasks NOT parked at a gate -- gate liveness belongs to
    the distinct adjudicator seat, never this observer (AC-4)."""
    tasks = task_svc.list(status="in_progress")
    return [t for t in tasks
            if (getattr(t, "gate_state", "none") or "none") == "none"]


def observe_tick(task_svc, cond, scores_db: str,
                  tokens_reader: Optional[Callable] = None) -> list[str]:
    """One liveness sweep. For every in_progress, non-gate-parked task
    with a linked session, compares its transcript token count against
    the PREVIOUS tick's reading for that task_id and beats
    drive_heartbeat ONLY when the reading GREW.

    A task's first-ever observation never beats (growth needs two
    samples, AC-4). Returns the task_ids actually beaten this tick.
    """
    reader = tokens_reader or (
        lambda tid, sid: _default_tokens_reader(tid, sid, cond))

    beaten: list[str] = []
    for task in _candidate_tasks(task_svc):
        sessions = task_svc.sessions_for_task(task.id)
        if not sessions:
            continue
        session_id = sessions[-1].get("session_id") or ""
        if not session_id:
            continue

        try:
            current = reader(task.id, session_id)
        except Exception:
            logging.getLogger(__name__).debug(
                "drive_activity_observer: tokens_reader raised for %s",
                task.id, exc_info=True)
            continue
        if current is None:
            continue

        previous = _last_tokens.get(task.id)
        _last_tokens[task.id] = current
        if previous is None or current <= previous:
            continue

        result = drive_heartbeat.record_heartbeat(scores_db, {
            "task_id": task.id,
            "step": task.workflow_step or "unknown",
            "elapsed_s": 0,
            "last_tool": "observed_transcript_growth",
            "work_units": current,
        })
        if result.get("ok"):
            beaten.append(task.id)
    return beaten


def sweep_once() -> list[str]:
    """One pass over every project (mirrors gate_adjudicator/task_runner's
    own sweep_once shape): run observe_tick against each project's own
    task_svc/conductor_svc/scores_db."""
    from prism_service.config import project_data_dir
    from prism_service.project_context import get_all_projects, get_project

    beaten_all: list[str] = []
    for pid in get_all_projects():
        try:
            ctx = get_project(pid)
            scores_db = str(project_data_dir(pid) / "scores.db")
            beaten = observe_tick(ctx.task_svc, ctx.conductor_svc, scores_db)
        except Exception as exc:
            _log(f"{pid}: sweep failed ({exc})")
            continue
        beaten_all.extend(beaten)
    return beaten_all


def _loop(interval_s: int) -> None:
    _log(f"started; interval={interval_s}s")
    while True:
        try:
            sweep_once()
        except Exception as exc:
            _log(f"sweep error: {exc}")
        time.sleep(interval_s)


def start_drive_activity_observer(
        interval_s: Optional[int] = None) -> Optional[threading.Thread]:
    """Spawn the background sweep thread (idempotent -- a second call
    while one is already running and alive is a no-op). Disabled only via
    PRISM_DRIVE_ACTIVITY_OBSERVER_INTERVAL=0; default ON."""
    global _thread
    if interval_s is None:
        interval_s = _interval_s()
    if interval_s <= 0:
        _log("disabled (PRISM_DRIVE_ACTIVITY_OBSERVER_INTERVAL=0)")
        return None
    if _thread is not None and _thread.is_alive():
        return _thread
    _thread = threading.Thread(
        target=_loop, args=(interval_s,),
        name="drive-activity-observer", daemon=True)
    _thread.start()
    return _thread
