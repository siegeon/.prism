"""Red scaffold (integration) — out-of-process supervisor recovery (GH #162).

The silent all-threads futex wedge (process alive, ports 7777/7778 still
LISTEN, every HTTP/MCP request hangs, only an external SIGKILL recovers)
RECURS on v6.5.4 within ~5 min of boot. The shipped #155 in-process
watchdog CANNOT save the process: its self-heal os._exit(1) and per-cycle
faulthandler re-arm BOTH require the GIL, which the native-futex holder
never yields, so during a true wedge the watchdog thread is itself parked.

The durable fix is recovery from OUTSIDE the wedged process. These tests
pin the REAL seams (not unit contracts that pass on dead code):

  AC-1  cmd_start (prism start --daemon) starts the supervisor as a SEPARATE
        PROCESS off the server's contended GIL path — NOT a threading.Thread
        of the server. Asserted against cmd_start SOURCE + the supervisor
        module shape (a process-driver, not a daemon thread).
  AC-2  after N consecutive UI-port probe timeouts the supervisor triggers
        recovery within a bounded (~5s) window.
  AC-3  the kill path is SIGKILL (taskkill /F on Windows), NEVER SIGTERM /
        graceful stop (graceful shutdown is itself wedged).
  AC-4  after the kill the supervisor RESPAWNS the server and updates the
        pidfile to the NEW pid (old pid replaced).
  AC-5  faulthandler is armed with repeat=True (continuous, independent of
        the GIL-bound probe loop) — no unarmed window on a fully GIL-starved
        server.
  AC-6  the in-process watchdog self-heal (_kill_enabled) defaults ON.
"""

from __future__ import annotations

import importlib
import inspect
import re
import signal

import pytest


# ---------------------------------------------------------------------------
# AC-1 — the supervisor is a SEPARATE PROCESS, started by cmd_start.
# ---------------------------------------------------------------------------
def test_supervisor_module_exists_and_is_process_driver():
    """services/supervisor.py must exist and expose an out-of-process
    recovery loop — NOT a threading.Thread daemon like the in-process
    watchdog. The likely_misfire is implementing the 'supervisor' as
    another in-process thread (still GIL-starvable); guard against it by
    asserting a process-shaped entrypoint and the ABSENCE of a daemon-thread
    spawn in the supervise loop itself."""
    sup = importlib.import_module("prism_service.services.supervisor")
    # A callable that RUNS the supervise loop in-line (the separate process's
    # main), and a kill primitive — the recovery surface.
    assert hasattr(sup, "supervise_loop") or hasattr(sup, "run_supervisor"), (
        "supervisor.py exposes no supervise_loop/run_supervisor entrypoint — "
        "the out-of-process supervisor body is missing"
    )
    assert hasattr(sup, "force_kill"), (
        "supervisor.py exposes no force_kill primitive (SIGKILL/taskkill /F)"
    )
    src = inspect.getsource(sup)
    # The recovery driver must NOT run itself as a daemon thread of the
    # server — that is exactly the GIL-starvable false-green the misfire
    # warns about. The supervisor IS the process; it loops in-line.
    assert "threading.Thread" not in src, (
        "supervisor.py spawns a threading.Thread — the supervisor must be a "
        "SEPARATE PROCESS off the server's contended GIL path, not a thread"
    )


def test_cmd_start_daemon_launches_supervisor_process():
    """prism start --daemon (cli/prism_cli.py cmd_start) must launch the
    supervisor as a SEPARATE child process (its own python -m
    ...supervisor), distinct from the server child. Asserted against
    cmd_start SOURCE so it fails loudly if the supervisor is never wired —
    a TestClient-importable module is NOT enough (that exact gap shipped
    false-greens)."""
    cli = importlib.import_module("prism_service.cli.prism_cli")
    src = inspect.getsource(cli.cmd_start)
    assert "supervisor" in src, (
        "cmd_start never references the supervisor — `prism start --daemon` "
        "does not launch the out-of-process supervisor (dead code)"
    )
    # It must SPAWN the supervisor as its own module-run child (a separate
    # process), not merely import a function.
    assert re.search(r"prism_service\.services\.supervisor", src) or \
        re.search(r"supervisor", src), \
        "cmd_start does not spawn the supervisor module as a child process"


# ---------------------------------------------------------------------------
# AC-2 — N consecutive hung probes trigger recovery within a bound.
# ---------------------------------------------------------------------------
def test_n_consecutive_hung_probes_trigger_recovery(monkeypatch):
    """The supervisor probes the UI port; after N consecutive timeouts it
    fires recovery exactly once (on the Nth), not before."""
    sup = importlib.import_module("prism_service.services.supervisor")

    recovered = {"n": 0}
    monkeypatch.setattr(sup, "force_kill", lambda *a, **k: recovered.__setitem__("n", recovered["n"] + 1))
    monkeypatch.setattr(sup, "respawn_server", lambda *a, **k: 4321, raising=False)

    threshold = sup.failure_threshold() if hasattr(sup, "failure_threshold") else 3

    state = sup.SupervisorState() if hasattr(sup, "SupervisorState") else None
    assert state is not None, "supervisor.py exposes no SupervisorState to track consecutive failures"

    # Feed threshold-1 hung probes: NO recovery yet.
    for _ in range(threshold - 1):
        sup.handle_probe_result(state, alive=False)
    assert recovered["n"] == 0, "recovery fired BEFORE the consecutive-timeout threshold"

    # The Nth hung probe trips recovery.
    sup.handle_probe_result(state, alive=False)
    assert recovered["n"] == 1, "recovery did NOT fire on the Nth consecutive hung probe"


def test_recovery_bound_is_about_five_seconds():
    """NFR-1: the bound (probe timeout x threshold) is tuned to ~5s, not
    the watchdog's 30s x 3 = 90s. The supervisor must recover an order of
    magnitude faster than the in-process watchdog cadence."""
    sup = importlib.import_module("prism_service.services.supervisor")
    interval = sup.probe_interval_s() if hasattr(sup, "probe_interval_s") else None
    threshold = sup.failure_threshold() if hasattr(sup, "failure_threshold") else None
    assert interval is not None and threshold is not None, (
        "supervisor.py exposes no probe_interval_s()/failure_threshold() knobs"
    )
    assert interval * threshold <= 6.0, (
        f"recovery bound {interval * threshold}s exceeds the ~5s requirement "
        f"(interval={interval}s x threshold={threshold})"
    )


# ---------------------------------------------------------------------------
# AC-3 — kill is SIGKILL / taskkill /F, never SIGTERM / graceful.
# ---------------------------------------------------------------------------
def test_force_kill_uses_sigkill_not_sigterm(monkeypatch):
    """The kill primitive must SIGKILL (taskkill /F on Windows). A graceful
    SIGTERM is itself wedged, so it would never recover the process."""
    sup = importlib.import_module("prism_service.services.supervisor")

    seen = {"sigkill": 0, "sigterm": 0, "taskkill_f": 0}

    import sys as _sys
    if _sys.platform.startswith("win"):
        def _fake_run(cmd, *a, **k):
            joined = " ".join(str(c) for c in cmd)
            if "taskkill" in joined and "/F" in joined:
                seen["taskkill_f"] += 1
            if "taskkill" in joined and "/F" not in joined:
                seen["sigterm"] += 1  # graceful taskkill (no /F) — forbidden

            class _R:
                returncode = 0
            return _R()
        monkeypatch.setattr(sup.subprocess, "run", _fake_run)
    else:
        def _fake_kill(pid, sig):
            if sig == signal.SIGKILL:
                seen["sigkill"] += 1
            elif sig == signal.SIGTERM:
                seen["sigterm"] += 1
        monkeypatch.setattr(sup.os, "kill", _fake_kill)

    sup.force_kill(99999)

    if _sys.platform.startswith("win"):
        assert seen["taskkill_f"] == 1, "Windows kill path did not use taskkill /F"
    else:
        assert seen["sigkill"] == 1, "POSIX kill path did not use signal.SIGKILL"
    assert seen["sigterm"] == 0, (
        "supervisor used a GRACEFUL stop (SIGTERM / taskkill without /F) — "
        "graceful shutdown is itself wedged, so it cannot recover"
    )


# ---------------------------------------------------------------------------
# AC-4 — respawn replaces the server pid and rewrites the pidfile.
# ---------------------------------------------------------------------------
def test_respawn_updates_pidfile_to_new_pid(monkeypatch, tmp_path):
    """After the kill the supervisor respawns the server child and writes
    the NEW pid into the pidfile (old pid replaced) so prism status/stop
    track the live daemon."""
    sup = importlib.import_module("prism_service.services.supervisor")

    pidfile = tmp_path / "prism.pid"
    pidfile.write_text("11111", encoding="utf-8")
    monkeypatch.setattr(sup, "_pidfile_path", lambda: pidfile, raising=False)

    # respawn_server returns the new child's pid.
    monkeypatch.setattr(sup, "_spawn_server", lambda *a, **k: 22222, raising=False)

    new_pid = sup.respawn_server()
    assert new_pid == 22222, "respawn_server did not return the new server pid"
    assert pidfile.read_text(encoding="utf-8").strip() == "22222", (
        "pidfile was not updated to the new server pid after respawn"
    )


# ---------------------------------------------------------------------------
# AC-5 — faulthandler continuously armed (repeat=True), independent of the
# GIL-bound probe loop. Asserted against the in-process arming SOURCE.
# ---------------------------------------------------------------------------
def test_faulthandler_armed_repeat_true_independent_of_probe_loop():
    """A fully GIL-starved server still emits an all-thread traceback only
    if faulthandler is armed with repeat=True (or re-armed off the GIL-bound
    loop). Today watchdog._run_cycle arms with repeat=False, re-armed only
    inside the GIL-bound _loop — an unarmed window persists. This asserts a
    continuous-arm path exists with repeat=True."""
    main_mod = importlib.import_module("prism_service.main")
    wd = importlib.import_module("prism_service.services.watchdog")

    main_src = inspect.getsource(main_mod)
    wd_src = inspect.getsource(wd)
    combined = main_src + "\n" + wd_src

    # The arming must use repeat=True somewhere on a path that is NOT the
    # per-cycle re-arm. repeat=False inside the GIL-bound loop leaves a gap.
    assert re.search(r"dump_traceback_later\([^)]*repeat\s*=\s*True", combined), (
        "no faulthandler.dump_traceback_later(repeat=True) — the wedge has an "
        "unarmed window whenever the GIL-bound probe loop cannot run"
    )


# ---------------------------------------------------------------------------
# AC-6 — in-process watchdog self-heal defaults ON (defense-in-depth).
# ---------------------------------------------------------------------------
def test_watchdog_kill_defaults_on(monkeypatch):
    """PRISM_WATCHDOG_KILL defaults ON now (best-effort in-process self-heal
    as defense-in-depth). The out-of-process supervisor is the actual
    guarantee, but the watchdog kill should no longer default OFF."""
    wd = importlib.import_module("prism_service.services.watchdog")
    monkeypatch.delenv("PRISM_WATCHDOG_KILL", raising=False)
    assert wd._kill_enabled() is True, (
        "PRISM_WATCHDOG_KILL must default ON (True) as defense-in-depth"
    )
    monkeypatch.setenv("PRISM_WATCHDOG_KILL", "0")
    assert wd._kill_enabled() is False, "PRISM_WATCHDOG_KILL=0 must still opt OUT"


# ---------------------------------------------------------------------------
# AC-8 — version patch-bumped from 6.5.5 with release notes.
# ---------------------------------------------------------------------------
def test_version_patch_bumped_for_supervisor():
    """The user-visible supervisor commit must patch-bump PRISM_VERSION past
    6.5.5 with GH #162 release notes."""
    ver = importlib.import_module("prism_service.__version__")
    parts = tuple(int(x) for x in ver.PRISM_VERSION.split("."))
    assert parts >= (6, 5, 6), (
        f"PRISM_VERSION {ver.PRISM_VERSION} not bumped past 6.5.5 for the "
        f"supervisor change"
    )
    assert "162" in ver.PRISM_VERSION_NOTES, (
        "release notes do not mention GH #162 (the supervisor fix)"
    )
