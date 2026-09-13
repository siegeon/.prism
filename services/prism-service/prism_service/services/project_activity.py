"""Tracks which projects have seen REAL CLIENT request activity recently.

The drift reindexer (main.py's start_drift_timer, task: livehang round 4)
used to sweep EVERY tracked project on a fixed cadence, including junk
slugs nobody ever opens -- and the very first tick after any restart
always fired immediately (last_reindex started at 0.0), pegging the CPU
and starving every other thread's access to the GIL for 20-30 minutes.
Owner directive (2026-09-13): "we only care about the project we have
open... stop doing work just for fun." This module is the signal the
drift timer uses to answer "is anyone actually looking at this project
right now" -- a thin, in-memory, lock-protected map, never persisted,
never touched by background workers themselves (only real HTTP requests
mark a project seen, via the middleware in main.py that reads the
`project` query param)."""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_last_seen: dict[str, float] = {}
_last_request_at: float = 0.0


def mark_seen(project_id: str) -> None:
    """Record that `project_id` had a real client request just now."""
    global _last_request_at
    if not project_id:
        return
    now = time.time()
    with _lock:
        _last_seen[project_id] = now
        _last_request_at = now


def seen_within(project_id: str, seconds: float) -> bool:
    """True when `project_id` has had a request within the last `seconds`."""
    with _lock:
        ts = _last_seen.get(project_id)
    return ts is not None and (time.time() - ts) < seconds


def idle_for(seconds: float) -> bool:
    """True when NO request, to ANY project, has landed in the last
    `seconds` -- the drift reindexer only runs while the API is quiet,
    never mid-burst. True before the very first request ever (nothing to
    be idle FROM yet)."""
    with _lock:
        last = _last_request_at
    return last == 0.0 or (time.time() - last) >= seconds


# Default "in use" window, shared by every caller of is_in_use() below --
# 10 minutes covers a normal working session with gaps for reading/
# thinking without re-triggering background work the instant a tab goes
# quiet.
DEFAULT_ACTIVE_WINDOW_S = 600.0


def is_in_use(project_id: str, active_window_s: float = DEFAULT_ACTIVE_WINDOW_S) -> bool:
    """True when `project_id` counts as "in use" right now: a real client
    request within `active_window_s`, OR a task actively in motion (an
    in-progress task means someone/something is working this project even
    without direct API traffic in that exact window). The one signal
    every "only do background work for the project actually open" gate
    shares -- drift_worker.sweep_once() and maintenance_clock._run_
    governance() both use this (task: livehang rounds 4-5)."""
    if seen_within(project_id, active_window_s):
        return True
    try:
        from prism_service.project_context import get_project
        return bool(get_project(project_id).task_svc.active_ids())
    except Exception:
        return False
