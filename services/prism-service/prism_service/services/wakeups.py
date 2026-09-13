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
import sqlite3
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

# ---------------------------------------------------------------------------
# Cross-process signalling (task: worker-host process split, 2026-09-13).
#
# The in-memory _LAST dict + threading.Condition above only ever reaches
# waiters in THIS process. Once PRISM_WORKERS_PROCESS=1 moves the standing
# workers into a separate OS process from the API, a mutation raised by an
# HTTP request (API process) must still wake a worker loop's wait() call
# running in the OTHER process. This is the belt: a tiny WAL-mode sqlite
# table under the data dir, written on every signal() and polled by wait()
# every _CROSS_POLL_S. It is deliberately dumb (last-write-wins per
# (kind, project), no queue, no fan-out bookkeeping) because wait() only
# ever asks "has anything newer than my baseline happened", exactly what
# the in-process path already answers.
# ---------------------------------------------------------------------------

_CROSS_POLL_S = 0.25
_CROSS_LOCK = threading.Lock()
_CROSS_CACHE: dict = {"conn": None, "path": None}


def _cross_db_path() -> Optional[str]:
    try:
        from prism_service.data_dir import resolve_data_dir
        return str(resolve_data_dir() / "wakeups.db")
    except Exception:
        return None


def _cross_conn() -> Optional[sqlite3.Connection]:
    """A cached, process-wide connection to the cross-process signal table.
    None (feature silently off) if the data dir cannot be resolved -- every
    caller treats that as "no cross-process peer to reach", never an error."""
    path = _cross_db_path()
    if not path:
        return None
    with _CROSS_LOCK:
        conn = _CROSS_CACHE.get("conn")
        if conn is not None and _CROSS_CACHE.get("path") == path:
            return conn
        try:
            conn = sqlite3.connect(path, timeout=1.0, isolation_level=None,
                                    check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS signals ("
                "kind TEXT NOT NULL, project TEXT NOT NULL, ts REAL NOT NULL, "
                "PRIMARY KEY (kind, project))"
            )
        except Exception:
            return None
        _CROSS_CACHE["conn"] = conn
        _CROSS_CACHE["path"] = path
        return conn


def _cross_signal(kind: str, project: str, ts: float) -> None:
    conn = _cross_conn()
    if conn is None:
        return
    try:
        with _CROSS_LOCK:
            conn.execute(
                "INSERT INTO signals(kind, project, ts) VALUES (?, ?, ?) "
                "ON CONFLICT(kind, project) DO UPDATE SET ts=excluded.ts",
                (kind, project, ts),
            )
    except Exception:
        pass


def _cross_has_new(kinds: set, project: Optional[str], baseline: float) -> bool:
    conn = _cross_conn()
    if conn is None:
        return False
    placeholders = ",".join("?" for _ in kinds)
    try:
        with _CROSS_LOCK:
            rows = conn.execute(
                f"SELECT project, ts FROM signals WHERE kind IN ({placeholders})",
                tuple(kinds),
            ).fetchall()
    except Exception:
        return False
    for ep, ts in rows:
        if project and ep != "*" and ep != project:
            continue
        if ts > baseline:
            return True
    return False


def signal(kind: str, project: str = "*", task_id: Optional[str] = None) -> None:
    """Record that `kind` happened for `project` and wake every waiter, in
    THIS process and (best-effort) any other process running a
    PRISM_WORKERS_PROCESS=1 worker host against the same data dir. Never
    raises, never blocks -- safe to call from any mutation path, including
    ones with no worker currently listening.

    ONE `time.time()` read is shared by both the in-memory record and the
    cross-process row (never two separate calls) -- a waiter that captures
    its own baseline between two different reads of "now" would otherwise
    see the earlier-timestamped channel as already-consumed while the
    later-timestamped one still reads as "new", double-firing the very
    next wait() on a signal that already woke this one (owner 2026-09-13:
    "one signal makes exactly one pass")."""
    now = time.time()
    try:
        with _COND:
            _LAST[(kind, project or "*")] = now
            _COND.notify_all()
    except Exception:
        pass
    _cross_signal(kind, project or "*", now)


def _has_new(kinds: set, project: Optional[str], baseline: float) -> bool:
    for (ek, ep), ts in _LAST.items():
        if ek not in kinds:
            continue
        if project and ep != "*" and ep != project:
            continue
        if ts > baseline:
            return True
    return False


def worker_fallback_s() -> Optional[float]:
    """Explicit opt-in ONLY: PRISM_WORKER_FALLBACK_S, unset by default.

    Owner 2026-09-13 ("it's all reactive and real time"): None (the
    default) means a worker's wait() below blocks until a REAL signal,
    with no periodic fallback wake at all -- not even a large one. Every
    standing worker in this reactive family shares this one env var and
    this one function rather than each inventing its own hardcoded
    interval/TTL/safety-net constant. Set it only for an environment
    whose writes genuinely bypass every signal path this family already
    covers (a direct DB poke, a push this process's hooks never see)."""
    raw = os.environ.get("PRISM_WORKER_FALLBACK_S", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def wait(kinds: Iterable[str], project: Optional[str] = None,
         timeout: Optional[float] = 900.0, since: Optional[float] = None) -> bool:
    """Block until one of `kinds` has signalled (for `project`, or any
    project when `project` is None/omitted -- a project-scoped worker
    should pass its own project so it still wakes on wildcard signals)
    more recently than `since`, or `timeout` seconds elapse.

    `timeout=None` blocks FOREVER until a real signal arrives -- no
    fallback wake at all. A worker loop should pass
    `timeout=worker_fallback_s()` (None unless an operator explicitly
    opted in) rather than a hardcoded interval, so "no signal" genuinely
    means "no work" instead of "check again in N seconds anyway."

    `since` defaults to "now" (the instant `wait()` was called) -- a
    worker that already swept as of some earlier timestamp should pass
    that timestamp so a signal raised WHILE it was still working is not
    missed. Returns True on a real wakeup, False on a plain timeout
    (never, when timeout is None).
    """
    kinds_set = set(kinds)
    baseline = time.time() if since is None else since
    deadline = None if timeout is None else time.time() + max(0.0, timeout)
    with _COND:
        while True:
            if _has_new(kinds_set, project, baseline):
                return True
            if _cross_has_new(kinds_set, project, baseline):
                return True
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                poll = min(remaining, _CROSS_POLL_S)
            else:
                poll = _CROSS_POLL_S
            # Wake at least every _CROSS_POLL_S (250ms) even with no local
            # notify_all(), so a signal() raised in ANOTHER process is
            # picked up within ~500ms round-trip (task: worker-host process
            # split) -- the in-process path is still notify_all()-driven
            # and typically wakes far sooner than this poll floor. This
            # poll checks the cross-process bridge only; it is not a
            # fallback wake of the CALLER's own sweep body.
            _COND.wait(timeout=poll)


def changed_since(kinds: Iterable[str], project: Optional[str],
                   baseline: float) -> list[tuple[str, str, float]]:
    """Which of `kinds` have a signal newer than `baseline` (for `project`,
    or every project when None) -- across BOTH the in-memory record and
    the cross-process table, deduped to the single newest (project, ts)
    per kind. Backs GET /sse/changes (routes/sse.py): after `wait()`
    returns True, this answers WHICH kind(s) actually moved, so the
    stream can emit one real event per change instead of a bare "something
    happened" ping the client would have to re-poll to interpret."""
    kinds_set = set(kinds)
    best: dict[str, tuple[str, float]] = {}
    with _LOCK:
        for (ek, ep), ts in _LAST.items():
            if ek not in kinds_set or ts <= baseline:
                continue
            if project and ep != "*" and ep != project:
                continue
            if ek not in best or ts > best[ek][1]:
                best[ek] = (ep, ts)
    conn = _cross_conn()
    if conn is not None:
        placeholders = ",".join("?" for _ in kinds_set) or "NULL"
        try:
            with _CROSS_LOCK:
                rows = conn.execute(
                    f"SELECT kind, project, ts FROM signals WHERE kind IN ({placeholders})",
                    tuple(kinds_set),
                ).fetchall()
        except Exception:
            rows = []
        for ek, ep, ts in rows:
            if ts <= baseline:
                continue
            if project and ep != "*" and ep != project:
                continue
            if ek not in best or ts > best[ek][1]:
                best[ek] = (ep, ts)
    return [(k, p, t) for k, (p, t) in best.items()]


def changes_snapshot(project: Optional[str] = None) -> float:
    """The newest signal timestamp visible to `project` (any kind, plus
    wildcard signals) -- or across every project when `project` is None.

    Backs GET /api/changes (api/changes.py): the SPA's shared poll layer
    runs ONE 1s poll of that tiny endpoint per tab, and every other data
    query gates its own refetch on "did this number move" instead of a
    private fixed-interval timer. Monotonic non-decreasing for a fixed
    `project` across the process lifetime -- a fresh maximum over the same
    `_LAST` dict `_has_new`/`last_signal_at` already read, so this mints no
    new state of its own."""
    with _LOCK:
        best = 0.0
        for (_ek, ep), ts in _LAST.items():
            if project and ep != "*" and ep != project:
                continue
            if ts > best:
                best = ts
        return best


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
    this module-level state -- including the cross-process sqlite table,
    which otherwise outlives any one test (the suite pins PRISM_DATA_DIR
    to one throwaway dir for the whole session, not per-test)."""
    with _LOCK:
        _LAST.clear()
    conn = _cross_conn()
    if conn is not None:
        try:
            with _CROSS_LOCK:
                conn.execute("DELETE FROM signals")
        except Exception:
            pass


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
