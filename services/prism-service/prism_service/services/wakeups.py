"""In-process event bus so the daemon's standing background workers can
wake on a REAL event instead of polling a fixed interval.

Owner directive 2026-09-13: nine standing workers (task_runner,
gate_adjudicator, resume_actuator, deploy_worker, dispatch_guard,
language_alignment_worker, maintenance_clock, ship_worker, the main.py
drift loop) were all ticking on fixed intervals whether or not anything
changed, so the Live page's System Activity panel (system_activity.py)
climbed on an idle system and read as churn -- "we only care about the
project we have open ... visibility of all processing" was read as "the
app is doing something" when it was mostly a clock ticking over nothing.

This module is the wakeup half of the fix: a mutation point (a task
status/step/gate change, a landed ship, a workspace write, a minted
evidence receipt) calls `signal(kind, project, task_id=...)`, and a
worker loop calls `wait([...kinds], project=..., timeout=<fallback>)`
instead of `time.sleep(interval)`. A signal wakes every waiter within
milliseconds (a `threading.Condition.notify_all()`, not a poll loop);
the timeout survives ONLY as a safety-net upper bound for whatever this
module never learns about (a direct DB write, crash recovery, a
different process entirely).

Pure in-memory, single process. A signal raised in one process is never
seen by another (e.g. a separate `claude -p` step agent) -- workers that
must react across processes still need their own channel; this only
covers the one daemon process's own standing workers.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from typing import Iterable, Iterator, Optional

_LOCK = threading.Lock()
_COND = threading.Condition(_LOCK)

# (kind, project) -> last signal time. "*" as project means "any project";
# a waiter scoped to project "prism" also wakes on a "*" signal of a kind
# it is watching, since most mutation points don't always know a caller's
# project (or the signal is genuinely project-agnostic).
_LAST: dict[tuple[str, str], float] = {}


def signal(kind: str, project: str = "*", task_id: Optional[str] = None) -> None:
    """Record that `kind` happened for `project` and wake every waiter.
    Never raises, never blocks -- safe to call from any mutation path,
    including ones with no worker currently listening."""
    try:
        with _COND:
            _LAST[(kind, project or "*")] = time.time()
            _COND.notify_all()
    except Exception:
        pass


def _has_new(kinds: set, project: Optional[str], baseline: float) -> bool:
    for (ek, ep), ts in _LAST.items():
        if ek not in kinds:
            continue
        if project and ep != "*" and ep != project:
            continue
        if ts > baseline:
            return True
    return False


def wait(kinds: Iterable[str], project: Optional[str] = None,
         timeout: float = 900.0, since: Optional[float] = None) -> bool:
    """Block until one of `kinds` has signalled (for `project`, or any
    project when `project` is None/omitted -- a project-scoped worker
    should pass its own project so it still wakes on wildcard signals)
    more recently than `since`, or `timeout` seconds elapse.

    `since` defaults to "now" (the instant `wait()` was called) -- a
    worker that already swept as of some earlier timestamp should pass
    that timestamp so a signal raised WHILE it was still working is not
    missed. Returns True on a real wakeup, False on a plain timeout.
    """
    kinds_set = set(kinds)
    baseline = time.time() if since is None else since
    deadline = time.time() + max(0.0, timeout)
    with _COND:
        while True:
            if _has_new(kinds_set, project, baseline):
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            # Wake at least every 5s even with no signal, so a very long
            # fallback timeout still re-checks promptly if `_LAST` was
            # mutated by a signal() that raced notify_all() (belt+braces;
            # notify_all() already covers the common case).
            _COND.wait(timeout=min(remaining, 5.0))


def last_signal_at(kind: str, project: Optional[str] = None) -> float:
    """Most recent signal time for `kind` (matching `project`, or the
    newest across all projects when None). 0.0 if never signalled.
    Test/diagnostic helper -- workers should use `wait()`, not this."""
    with _LOCK:
        best = 0.0
        for (ek, ep), ts in _LAST.items():
            if ek != kind:
                continue
            if project and ep != "*" and ep != project:
                continue
            if ts > best:
                best = ts
        return best


def _reset_for_tests() -> None:
    """Test-only: clear all recorded signals between tests that share
    this module-level state."""
    with _LOCK:
        _LAST.clear()


# ---------------------------------------------------------------------------
# Startup warmup (owner 2026-09-13, live measurement on the AOS instance):
# every standing worker's very first tick fired within the same ~20s window
# after a restart -- gate_adjudicator, task_runner, resume_actuator (21.5s),
# ship_worker (13.0s + 8.9s), language_alignment (19.5s), maintenance_clock's
# brain_jobs (20.0s) and drift_reindex (8.4s) all overlapped, CPU sat at
# 150%, and /api/workflows/conductor/state/work/graph each took 10-25s
# while the pile-up lasted. None of that work is urgent in the first couple
# of minutes after a restart -- nothing has had time to change yet.
# ---------------------------------------------------------------------------

# A proxy for "process start": the first time this module is imported, which
# happens from each worker's own start_*()/​_loop(), all called within the
# same lifespan startup -- close enough that a shared constant is simpler
# and safer than threading an actual start timestamp through every caller.
_PROCESS_START = time.time()

DEFAULT_WARMUP_S = 120.0


def worker_warmup_s() -> float:
    raw = os.environ.get("PRISM_WORKER_WARMUP_S", "")
    try:
        return max(0.0, float(raw)) if raw.strip() else DEFAULT_WARMUP_S
    except ValueError:
        return DEFAULT_WARMUP_S


def wait_out_startup_warmup() -> None:
    """Block until PRISM_WORKER_WARMUP_S (default 120s) have elapsed since
    this module was first imported. Called ONCE by each worker before its
    very first tick, unconditionally -- a wakeup signal arriving during
    the window does not shorten it (the first tick waits for the LATER of
    "a signal arrived" and "warmup elapsed"; during warmup nothing has had
    time to change anyway, so there is nothing genuinely urgent for a
    signal to short-circuit). A worker started well after process start
    (warmup already elapsed) returns immediately."""
    remaining = (_PROCESS_START + worker_warmup_s()) - time.time()
    if remaining > 0:
        time.sleep(remaining)


def lower_thread_priority() -> None:
    """Best-effort: lower the CALLING thread's OS scheduling priority.
    Linux implements `nice()`/`setpriority()` per kernel schedulable entity
    (NPTL gives each thread its own tid), so calling this from inside a
    worker's own background thread affects only that thread, not the
    whole process -- unlike POSIX's nominal per-process semantics. A
    background worker never needs to win a scheduling race against a
    request-serving thread. No-op (never raises) on platforms without
    os.nice (e.g. Windows)."""
    try:
        os.nice(10)
    except Exception:
        pass


# Only one worker tick may run at a time, process-wide. Owner measurement
# above: 6+ standing workers each doing real CPU-bound work (git calls,
# difflib-style scans, per-task iteration) overlapped in the same ~20s
# window and contended hard enough for the GIL that ordinary HTTP routes
# (not just work.py's own known-slow ones) took 10-25s. Serializing tick
# BODIES (never the wait() in between) turns "6 things fighting for the
# GIL at once" into "6 things queued, one at a time" -- each still finishes
# in its own time, but never at the cost of every other thread's turn.
_SERIAL_LOCK = threading.Lock()


@contextlib.contextmanager
def serial_slot() -> Iterator[None]:
    """Wrap ONE worker's tick body (sweep_once()/run_tick()/etc -- never
    the wait() that follows it) so at most one background worker pass
    executes at a time across the whole process."""
    with _SERIAL_LOCK:
        yield
