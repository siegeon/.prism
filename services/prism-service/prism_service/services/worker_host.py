"""The standing background workers, run in a SEPARATE OS PROCESS from the
API (task: worker-host process split, owner 2026-09-13).

STRUCTURAL CAUSE this closes: the nine standing workers below ran as
threads INSIDE the FastAPI process, so any tick that held the GIL (rubric
scoring, embedding, diffing) or blocked on a git call delayed every HTTP
request the API was serving at the same moment -- measured live: ordinary
routes took 10-20s whenever a worker pass ran, 0.2s otherwise.

FIX: when `PRISM_WORKERS_PROCESS=1` (see `main._workers_process_enabled`,
default on for `prism start` in dev mode, off otherwise), `main.lifespan`
spawns exactly one child process via `multiprocessing.get_context("spawn")`
running `main()` below, and starts NONE of these nine threads itself.
`PRISM_WORKERS_PROCESS=0` keeps today's in-process-thread behavior
unchanged -- this module is then never imported by the API at all.

Cross-process plumbing the workers still need, now that they no longer
share a process (or its GIL/memory) with the API:
  - `wakeups.signal()`/`wakeups.wait()` already bridge across processes via
    a small sqlite table (see that module) -- a mutation raised by an HTTP
    request in the API process still wakes a worker loop's `wait()` here.
  - `system_activity.start_activity_exporter()` (started below) periodically
    writes this process's activity feed to a JSON file the API's own
    `system_activity.snapshot()` merges in, so the Live page's System
    Activity panel keeps showing these passes as they happen.

Each worker's own sweep logic is UNCHANGED and untouched by this module --
only WHERE it is started moved. `worker_names()` is the single source of
truth both this module and `main.py`'s startup-visibility bookkeeping read,
so a new standing worker is declared once.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import Callable, List, Tuple

_log = logging.getLogger("prism.worker_host")

# Each entry starts its own thread(s) and returns immediately -- exactly
# the calls main.py's lifespan made directly before this module existed.
# Lazy imports (inside the closures below, not at module import time) so
# importing `worker_host` to just check `worker_names()` from the API
# process never pulls in every worker's own dependency tree.


def _start_task_runner() -> None:
    from prism_service.services.task_runner import (
        release_stale_seat_leases, start_task_runner,
    )
    # Task b490fabc — drop stale leases from a prior process BEFORE either
    # seat below can take a new one; once, at startup, never per-sweep.
    release_stale_seat_leases()
    start_task_runner()


def _start_gate_adjudicator() -> None:
    from prism_service.services.gate_adjudicator import start_gate_adjudicator
    start_gate_adjudicator()


def _start_resume_actuator() -> None:
    from prism_service.services.resume_actuator import start_resume_actuator
    start_resume_actuator()


def _start_deploy_worker() -> None:
    from prism_service.services.deploy_worker import start_deploy_worker
    start_deploy_worker()


def _start_dispatch_guard() -> None:
    from prism_service.services.dispatch_guard import start_dispatch_reaper
    start_dispatch_reaper()


def _start_language_alignment_worker() -> None:
    from prism_service.services.language_alignment_worker import (
        start_language_alignment_worker,
    )
    start_language_alignment_worker()


def _start_maintenance_clock() -> None:
    from prism_service.services.maintenance_clock import start_maintenance_clock
    start_maintenance_clock()


def _start_ship_worker() -> None:
    from prism_service.services.ship_worker import start_ship_worker
    start_ship_worker()


def _start_drift_timer() -> None:
    # Defined in main.py (a thin while-True wrapper around
    # drift_worker.sweep_once()) -- imported lazily and wrapped in its own
    # thread here, same as main.py's lifespan did directly before this
    # module existed. Importing main.py pulls in the FastAPI app/uvicorn;
    # harmless (one-time cost in a fresh child process that never binds a
    # port), but kept out of every OTHER starter above so PRISM_WORKERS_
    # PROCESS=0's code path never pays it.
    from prism_service.main import start_drift_timer
    threading.Thread(target=start_drift_timer, daemon=True,
                      name="prism-drift-timer").start()


# name -> starter. The name is what `main.py`'s CORE_WORKERS-style
# bookkeeping and this module's own logging refer to; it is NOT required to
# match the underlying thread's target __qualname__.
_STARTERS: "List[Tuple[str, Callable[[], None]]]" = [
    ("task_runner", _start_task_runner),
    ("gate_adjudicator", _start_gate_adjudicator),
    ("resume_actuator", _start_resume_actuator),
    ("deploy_worker", _start_deploy_worker),
    ("dispatch_guard", _start_dispatch_guard),
    ("language_alignment_worker", _start_language_alignment_worker),
    ("maintenance_clock", _start_maintenance_clock),
    ("ship_worker", _start_ship_worker),
    ("drift_timer", _start_drift_timer),
]


def worker_names() -> List[str]:
    """The standing workers this host starts, in start order. Pure data --
    safe to call from the API process (no imports of the workers
    themselves) to answer "what runs where" without spawning anything."""
    return [name for name, _ in _STARTERS]


def start_all_workers() -> None:
    """Start every standing worker's own thread(s). Never raises -- a
    single worker failing to start is logged and skipped so the rest of
    the host still comes up; the alternative (one bad import kills the
    whole child process) loses every OTHER worker too."""
    for name, starter in _STARTERS:
        try:
            starter()
        except Exception:
            _log.critical("worker host: %r failed to start", name,
                           exc_info=True)


def _cpu_governor(stop: threading.Event, poll_s: float = 5.0,
                   cpu_pct_threshold: float = 50.0, window_s: float = 10.0,
                   throttle_sleep_s: float = 5.0) -> None:
    """Belt and braces (task b490fabc/host-tight-loop) on top of the real
    fix in the worker loops' own wait() calls: if some OTHER self-feedback
    bug like it ever slips back in, catch it live instead of pegging the
    box silently. Samples this PROCESS's own CPU every `poll_s`; if it
    averages over `cpu_pct_threshold` across a full `window_s` while
    `wakeups` has recorded no new signal at all in that window (in-process
    OR cross-process -- see `wakeups.changes_snapshot`), something is
    busy-looping with no real work to justify it. Logs one line, records a
    `system_activity` "throttled" entry (visible on the Live page), and
    sleeps `throttle_sleep_s` before resuming -- never raises, never exits
    the process. The four thresholds default to the production values;
    tests pass smaller ones so a real subprocess can exercise this in
    well under a second instead of the real 10s+ window."""
    try:
        import resource
    except ImportError:
        return  # Windows: no RUSAGE_SELF; the real fix still stands.
    from prism_service.services import wakeups

    def _cpu_seconds() -> float:
        u = resource.getrusage(resource.RUSAGE_SELF)
        return u.ru_utime + u.ru_stime

    last_cpu = _cpu_seconds()
    last_wall = time.monotonic()
    last_signal_ts = wakeups.changes_snapshot()
    high_cpu_since: "float | None" = None

    while not stop.wait(timeout=poll_s):
        cpu_now = _cpu_seconds()
        wall_now = time.monotonic()
        wall_delta = wall_now - last_wall
        cpu_pct = 100.0 * (cpu_now - last_cpu) / wall_delta if wall_delta > 0 else 0.0
        signal_ts = wakeups.changes_snapshot()
        signalled = signal_ts > last_signal_ts
        last_cpu, last_wall, last_signal_ts = cpu_now, wall_now, signal_ts

        if cpu_pct <= cpu_pct_threshold or signalled:
            high_cpu_since = None
            continue
        if high_cpu_since is None:
            high_cpu_since = wall_now
            continue
        if wall_now - high_cpu_since < window_s:
            continue

        measured_window_s = wall_now - high_cpu_since
        _log.warning(
            "worker host CPU governor: %.0f%% CPU over %.0fs with zero "
            "signals -- throttling %.0fs (pid=%s)",
            cpu_pct, measured_window_s, throttle_sleep_s, os.getpid(),
        )
        try:
            from prism_service.services import system_activity
            system_activity.record(
                "throttled", "*",
                f"worker host: {cpu_pct:.0f}% cpu, "
                f"{measured_window_s:.0f}s no signals",
                started_at=time.time(), elapsed_ms=0.0, ok=True,
            )
        except Exception:
            pass
        time.sleep(throttle_sleep_s)
        high_cpu_since = None


def _install_signal_handlers(stop: threading.Event) -> None:
    def _on_term(_signum, _frame) -> None:
        stop.set()
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_term)
        except (ValueError, OSError):
            pass  # not the main thread on this platform -- best effort


def main(data_dir: str, env: "dict[str, str] | None" = None) -> None:
    """Entry point for the child process (the `multiprocessing.Process`
    target `main.lifespan` spawns). Sets up its own environment/logging,
    starts every standing worker, starts the cross-process activity
    exporter, then blocks until asked to stop (SIGTERM/SIGINT — the API
    process terminates this one on its own shutdown).

    `env` is the parent's `os.environ` at spawn time: the "spawn" start
    method already gives the child a fresh interpreter with its own copy
    of the parent's environment, so this is normally redundant -- passed
    explicitly anyway (brief: `worker_host.main(data_dir, env)`) so the
    child's environment never depends on multiprocessing's own inheritance
    behavior, which differs across platforms/start methods.
    """
    if env:
        for k, v in env.items():
            os.environ.setdefault(k, v)
    os.environ["PRISM_DATA_DIR"] = data_dir
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s worker-host[%(process)d] %(levelname)s %(name)s: %(message)s",
    )
    _log.info("starting (pid=%s, data_dir=%s)", os.getpid(), data_dir)

    start_all_workers()

    try:
        from prism_service.services.system_activity import start_activity_exporter
        start_activity_exporter()
    except Exception:
        _log.warning("activity exporter failed to start", exc_info=True)

    stop = threading.Event()
    _install_signal_handlers(stop)
    threading.Thread(target=_cpu_governor, args=(stop,), daemon=True,
                      name="prism-worker-host-cpu-governor").start()
    while not stop.is_set():
        stop.wait(timeout=5.0)
    _log.info("stopping (pid=%s)", os.getpid())


if __name__ == "__main__":
    # Manual smoke-run: `python -m prism_service.services.worker_host`.
    main(os.environ.get("PRISM_DATA_DIR", os.path.expanduser("~/.prism")))
