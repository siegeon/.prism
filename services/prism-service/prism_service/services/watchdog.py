"""Deadlock watchdog (GH #155) — a deadlock-immune dump + health probe.

The silent wedge raises no fatal signal, so `faulthandler.enable()` never
dumps. This watchdog closes that gap. Each cycle it:

  1. ARMS `faulthandler.dump_traceback_later(timeout, repeat=False)` against
     prism.log — a C-level timer that fires even when EVERY Python thread is
     deadlocked on the GIL.
  2. Runs a REAL in-process HTTP self-probe `GET http://127.0.0.1:{UI}/`
     with a socket timeout.
  3. On a HEALTHY probe: `cancel_dump_traceback_later()` + record latency.
     On a HANGING probe: does NOT cancel — the C timer is left to fire and
     dump all thread stacks into prism.log (the diagnostic #155 asks for).

Opt-in self-heal: when PRISM_WATCHDOG_KILL=1 and consecutive_failures
exceeds the threshold, after dumping the watchdog calls os._exit(1) so a
supervisor (systemd) restarts the wedged process (graceful shutdown is
itself deadlocked). Default OFF.

Mirrors the maintenance-clock worker shape (is_enabled env tri-state,
module-level status, _loop, start_<worker> daemon entrypoint).
"""

from __future__ import annotations

import faulthandler
import http.client
import os
import sys as _sys
import threading
import time

WORKER_ID = "watchdog"
WORKER_LABEL = "Deadlock watchdog"

# A busy-but-HEALTHY daemon (model load, indexing, a maintenance pass) can
# block the single event loop for tens of seconds, making the HTTP self-probe
# time out. That is NOT a deadlock. So the kill fires only after the wedge has
# persisted CONTINUOUSLY for KILL_AFTER_S (env PRISM_WATCHDOG_KILL_AFTER_S) AND
# the process is past its startup GRACE_S (env PRISM_WATCHDOG_GRACE_S) — startup
# is when model-load + first-index legitimately stall the loop. A real all-
# threads deadlock is permanent, so it still trips; a transient busy spell never
# does. (GH #155 watchdog + #162 kill-default-on follow-up: stop os._exit-ing a
# healthy daemon.) Back-compat constant retained; no longer the kill trigger.
KILL_THRESHOLD = 3
DEFAULT_GRACE_S = 180
DEFAULT_KILL_AFTER_S = 300


def _log(msg: str) -> None:
    print(f"[{WORKER_ID}] {msg}", file=_sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Status dict — read by GET /api/watchdog. Module-level + lock-guarded, one
# shared dict across cycles of the single watchdog thread.
# ---------------------------------------------------------------------------
# Wall-clock (epoch) the watchdog _loop started; 0.0 until the loop runs, which
# keeps direct _run_cycle() unit-test calls "past grace" (the prod behaviour the
# arm/cancel tests pin). Set once in _loop.
_started_at = 0.0
# Epoch of the FIRST failure in the current CONTINUOUS failure streak, or None
# when the last probe was healthy. The kill clock measures (now - _first_fail_at)
# so an intermittent busy spell (which recovers between cycles) never accrues.
_first_fail_at: "float | None" = None

_status_lock = threading.Lock()
_status: dict = {
    "last_probe_ok": None,
    "last_probe_latency_ms": None,
    "consecutive_failures": 0,
    "last_dump_at": None,
    "dump_count": 0,
    "restarts": 0,
    "armed": False,
}


def watchdog_status() -> dict:
    """A copy of the live status dict (last_probe_ok, last_probe_latency_ms,
    consecutive_failures, last_dump_at, dump_count, restarts, armed)."""
    with _status_lock:
        return dict(_status)


def _set(**kw) -> None:
    with _status_lock:
        _status.update(kw)


def _bump(field: str, delta: int = 1) -> int:
    with _status_lock:
        _status[field] = (_status.get(field) or 0) + delta
        return _status[field]


# ---------------------------------------------------------------------------
# Env tunables.
# ---------------------------------------------------------------------------
def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").lower()
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """The watchdog thread is on by default; set PRISM_WATCHDOG=off to disable."""
    return _env_truthy("PRISM_WATCHDOG", default=True)


def interval_s() -> int:
    return max(5, _int_env("PRISM_WATCHDOG_INTERVAL_S", 30))


def timeout_s() -> int:
    return max(1, _int_env("PRISM_WATCHDOG_TIMEOUT_S", 20))


def grace_s() -> int:
    """Startup grace: a slow/failed probe in the first grace_s after the loop
    starts never counts toward a kill — model-load + first-index legitimately
    stall the single event loop right after boot."""
    return max(0, _int_env("PRISM_WATCHDOG_GRACE_S", DEFAULT_GRACE_S))


def kill_after_s() -> int:
    """A wedge must persist CONTINUOUSLY at least this long before an (opt-in)
    kill fires — long enough to outlast any legitimate busy spell, short enough
    to recover a truly-deadlocked process."""
    return max(30, _int_env("PRISM_WATCHDOG_KILL_AFTER_S", DEFAULT_KILL_AFTER_S))


def _kill_enabled() -> bool:
    """In-process self-heal — os._exit(1) defense-in-depth (GH #162). Defaults
    ON (best-effort: it still needs the GIL the futex holder never yields, so
    the OUT-OF-PROCESS supervisor in services/supervisor.py remains the actual
    recovery guarantee). Set PRISM_WATCHDOG_KILL=0/off to opt out."""
    return _env_truthy("PRISM_WATCHDOG_KILL", default=True)


def _probe_port() -> int:
    from prism_service.config import UI_PORT
    return UI_PORT


# ---------------------------------------------------------------------------
# The real in-process HTTP self-probe. Raises on hang/failure; returns the
# latency in ms on success. Monkeypatched in the arm/cancel tests.
# ---------------------------------------------------------------------------
def _self_probe(timeout_s: float) -> float:
    """GET http://127.0.0.1:{UI_PORT}/ with a socket timeout. Returns the
    round-trip latency in milliseconds, or raises (TimeoutError / OSError)
    if the server is wedged or unreachable."""
    port = _probe_port()
    start = time.time()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
    try:
        conn.request("GET", "/")
        conn.getresponse().read()
    finally:
        conn.close()
    return (time.time() - start) * 1000.0


# ---------------------------------------------------------------------------
# One watchdog cycle — arm, probe, cancel-or-dump. The heart of #155.
# ---------------------------------------------------------------------------
def _dump_path():
    """The open prism.log file object to dump tracebacks into (falls back to
    stderr, which the daemon redirects to prism.log)."""
    try:
        from prism_service.data_dir import log_file
        return open(log_file(), "a", encoding="utf-8")
    except Exception:
        return _sys.stderr


def _run_cycle(timeout_s: int) -> None:
    """Arm faulthandler, run the self-probe, then cancel (healthy) or leave
    the timer armed (hang). On a hang the C-level timer fires and dumps all
    thread stacks even though every Python thread is deadlocked. A kill fires
    only after a CONTINUOUS wedge past startup grace (see KILL_AFTER_S/GRACE_S)
    so a busy-but-healthy daemon is never os._exit-ed."""
    global _first_fail_at
    fh_file = _dump_path()
    try:
        faulthandler.dump_traceback_later(timeout_s, repeat=False, file=fh_file)
    except Exception as exc:
        _log(f"could not arm faulthandler: {exc}")
    _set(armed=True)

    try:
        latency_ms = _self_probe(timeout_s)
    except Exception:
        now = time.time()
        fails = _bump("consecutive_failures")
        if _first_fail_at is None:
            _first_fail_at = now
        wedge_s = now - _first_fail_at
        if (now - _started_at) < grace_s():
            # Startup stall (model-load / first-index), NOT a deadlock: disarm
            # the dump to avoid boot-time stack-dump noise and never kill.
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:
                pass
            _set(last_probe_ok=False, armed=False)
            _log(f"probe slow during startup grace "
                 f"({int(now - _started_at)}s/{grace_s()}s) — not a wedge")
            return
        # Past grace: a genuine hang. Leave the C timer ARMED so it dumps every
        # thread's stack into prism.log — the #155 diagnostic the deadlocked
        # interpreter cannot produce itself.
        _set(last_probe_ok=False, last_dump_at=now, armed=False)
        _bump("dump_count")
        _log(f"probe HUNG (streak={fails}, {int(wedge_s)}s continuous); "
             f"faulthandler armed to dump")
        if _kill_enabled() and wedge_s >= kill_after_s():
            _bump("restarts")
            _log(f"continuous wedge {int(wedge_s)}s >= {kill_after_s()}s — "
                 f"os._exit(1) for supervisor restart")
            os._exit(1)
        return

    # HEALTHY probe: clear the failure streak, cancel the armed timer, record.
    _first_fail_at = None
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    _set(last_probe_ok=True, last_probe_latency_ms=latency_ms,
         consecutive_failures=0, armed=False)


# ---------------------------------------------------------------------------
# Daemon-thread entrypoint — mirrors start_maintenance_clock.
# ---------------------------------------------------------------------------
def _loop(initial_delay_s: float) -> None:
    global _started_at
    _started_at = time.time()
    if initial_delay_s > 0:
        time.sleep(initial_delay_s)
    _log(f"started; interval={interval_s()}s timeout={timeout_s()}s "
         f"grace={grace_s()}s kill={_kill_enabled()} kill_after={kill_after_s()}s")
    while True:
        try:
            _run_cycle(timeout_s())
        except Exception as exc:
            _log(f"cycle error: {exc}")
        time.sleep(interval_s())


def start_watchdog(initial_delay_s: float = 5.0) -> threading.Thread | None:
    """Spawn the single watchdog daemon thread, unless disabled via
    PRISM_WATCHDOG=off. Mirrors start_maintenance_clock."""
    if not is_enabled():
        _log("disabled (PRISM_WATCHDOG=off)")
        return None
    t = threading.Thread(
        target=_loop, args=(initial_delay_s,),
        name="prism-watchdog", daemon=True,
    )
    t.start()
    return t


def stop_watchdog() -> None:
    """Cancel any armed faulthandler timer on shutdown (the daemon thread is
    a daemon — it dies with the process; this just disarms the C timer)."""
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    _set(armed=False)
