"""Out-of-process daemon supervisor (GH #162) — the recovery GUARANTEE.

The silent all-threads futex wedge (process alive, ports 7777/7778 still
LISTEN, every HTTP/MCP request hangs forever) cannot be recovered from
INSIDE the wedged server: the in-process watchdog's os._exit(1) and its
per-cycle faulthandler re-arm BOTH need the GIL, which the native-futex
holder never yields, so the watchdog thread is itself parked (services/
watchdog.py). #155 + #157 confirmed this twice.

This module is the durable fix: a SEPARATE PROCESS — launched by
`prism start --daemon` (cli/prism_cli.py cmd_start) as its own
`python -m prism_service.services.supervisor` child — that HTTP-probes the
server's UI port off the server's contended GIL path. After N consecutive
probe timeouts it SIGKILLs the wedged server (taskkill /F on Windows;
graceful stop is itself wedged) and RESPAWNS it, updating the pidfile to
the new pid. A silent indefinite hang becomes a ~5s self-restart.

It is intentionally a PROCESS, not an in-process worker thread: such a
thread in the server shares the same starved GIL and would wedge
identically. The supervise loop runs IN-LINE in this process's main — there
is no daemon-thread spawn anywhere in this module (the recovery loop IS the
process).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

WORKER_ID = "supervisor"


def _log(msg: str) -> None:
    print(f"[{WORKER_ID}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Env tunables — mirror the watchdog's PRISM_WATCHDOG_* tri-state pattern.
# Recovery bound = probe_interval_s * failure_threshold, tuned to ~5s (NOT
# the watchdog's 30s x 3 = 90s) so a wedge self-restarts an order of
# magnitude faster than the in-process cadence.
# ---------------------------------------------------------------------------
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """The supervisor runs by default; PRISM_SUPERVISOR=off disables it."""
    raw = os.environ.get("PRISM_SUPERVISOR", "").lower()
    if raw in ("0", "off", "false", "no"):
        return False
    return True


def probe_interval_s() -> float:
    """Seconds between UI-port probes. Default 1s so interval*threshold ~5s."""
    return max(0.2, _float_env("PRISM_SUPERVISOR_INTERVAL_S", 1.0))


def failure_threshold() -> int:
    """Consecutive hung probes before recovery fires. Default 5 (x1s = ~5s)."""
    return max(1, _int_env("PRISM_SUPERVISOR_FAILURES", 5))


def probe_timeout_s() -> float:
    """Per-probe socket timeout — short so a hung server reads as a timeout
    quickly rather than blocking the bounded recovery window."""
    return max(0.2, _float_env("PRISM_SUPERVISOR_PROBE_TIMEOUT_S", 0.8))


def max_restarts() -> int:
    """Crash-loop backoff window: stop respawning after this many restarts if a
    fresh child keeps wedging immediately, so a genuinely crash-looping child
    backs off to systemd (Restart=always) instead of thrashing forever. Default
    is now a FINITE 5 (was 0/unlimited); set 0 to restore unlimited."""
    return max(0, _int_env("PRISM_SUPERVISOR_MAX_RESTARTS", 5))


def startup_grace_s() -> float:
    """Post-(re)spawn grace window (~30s, env-tunable via
    PRISM_SUPERVISOR_STARTUP_GRACE_S). A freshly (re)spawned server is still
    cold-booting (model load ~7-9s); hung probes that arrive WHILE the child is
    inside this window do NOT count toward a kill, so a slow boot is never
    killed mid-warmup. Re-applied on EVERY respawn, not just first boot. Must
    outlast probe_interval_s()*failure_threshold() (the recovery bound), else a
    slow boot would be killed before it could become healthy."""
    return max(0.0, _float_env("PRISM_SUPERVISOR_STARTUP_GRACE_S", 30.0))


# ---------------------------------------------------------------------------
# Recovery state — consecutive-failure tracking. Pure data, no thread.
# ---------------------------------------------------------------------------
class SupervisorState:
    """Tracks consecutive hung probes + restart bookkeeping for one server
    pid. Plain object (no thread, no lock) — the supervise loop is single-
    threaded and in-line in the supervisor process."""

    def __init__(self, server_pid: int = 0) -> None:
        self.server_pid = server_pid
        self.consecutive_failures = 0
        self.restarts = 0
        self.recovering = False
        # Monotonic timestamp of the last (re)spawn — the startup-grace anchor.
        # None means "no fresh boot to protect" (e.g. a state built for a
        # pid that was already up): grace is OFF until a (re)spawn stamps it.
        self.spawned_at: float | None = None

    def mark_spawned(self, now: float | None = None) -> None:
        """Stamp the startup-grace anchor at a (re)spawn. Re-applied on EACH
        respawn so every fresh child gets its own grace window."""
        self.spawned_at = time.monotonic() if now is None else now


def _restart_in_progress() -> bool:
    """True iff an auto-update restart is in flight (a FRESH restart sentinel
    exists). GH #181: during the re-exec drain + cold-boot the UI port hangs;
    without this the supervisor would fire a COMPETING kill+respawn and stack a
    second daemon family (the split-brain). We defer recovery while the sentinel
    is fresh; the re-exec'd server clears it on healthy boot, and the sentinel's
    TTL means a crashed restart can't disable recovery forever (NFR-2)."""
    try:
        from prism_service.data_dir import restart_in_progress
        return restart_in_progress()
    except Exception:
        return False


def _in_startup_grace(state: SupervisorState) -> bool:
    """True iff the freshly (re)spawned child is still inside its startup
    grace window. False when no spawn anchor is set (nothing to protect) or
    the grace has elapsed — so a never-healthy child is eventually killed."""
    anchor = getattr(state, "spawned_at", None)
    if not anchor:
        return False
    return (time.monotonic() - anchor) < startup_grace_s()


# ---------------------------------------------------------------------------
# The HTTP probe — off the server's contended path (separate process).
# ---------------------------------------------------------------------------
def probe_alive(port: int | str, timeout_s: float | None = None) -> bool:
    """GET http://127.0.0.1:{port}/api/version with a short socket timeout.
    Returns False on hang/timeout/refused — i.e. the server is wedged or
    down. 127.0.0.1 only (the WSL wslrelay trap, #64); stdlib urllib, no
    new dependency. Mirrors prism_cli._daemon_http_alive."""
    if timeout_s is None:
        timeout_s = probe_timeout_s()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/version", timeout=timeout_s,
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# The kill primitive — SIGKILL / taskkill /F, NEVER graceful (AC-3).
# ---------------------------------------------------------------------------
def force_kill(pid: int) -> None:
    """Hard-kill the wedged server. SIGKILL on POSIX, `taskkill /F /T` on
    Windows — NEVER SIGTERM / a graceful taskkill, because graceful shutdown
    is itself wedged (uvicorn's drain needs the GIL the futex holder owns).
    The /T tree-kill reaps the server's own children too."""
    if pid <= 0:
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"], check=False,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        _log(f"force_kill(pid={pid}) failed: {exc}")


# ---------------------------------------------------------------------------
# Process-level liveness — the STRONG respawn trigger (task 3576dd3f).
# Distinct from probe_alive(): answers "does the PROCESS exist", not "is it
# responsive". A dead watched pid is an unambiguous crash to recover from
# immediately (no waiting out N HTTP timeouts); a slow-but-ALIVE process is
# left to the graced HTTP-probe path so a busy daemon is never killed here.
# ---------------------------------------------------------------------------
def server_process_alive(pid: int) -> bool:
    """True iff PID names a live process. tasklist exact-PID match on Windows /
    os.kill(0) on POSIX; biases to True on PermissionError so we never respawn
    OVER a live process we merely cannot query. Mirrors cli._is_alive."""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=False,
        )
        import csv
        for row in csv.reader((out.stdout or "").splitlines()):
            if len(row) >= 2 and row[1].strip() == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------------
# Respawn — start a fresh server child + rewrite the pidfile (AC-4).
# ---------------------------------------------------------------------------
def _pidfile_path():
    from prism_service.data_dir import pid_file
    return pid_file()


def _read_pidfile_pid() -> int:
    """The pid the pidfile currently names (0 if absent/garbage). The SERVER
    rewrites this with its OWN pid on boot (main._own_pidfile_write), so it is
    the source of truth for which process the supervisor should watch."""
    try:
        return int(_pidfile_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_pid(pid: int) -> None:
    """Publish the pidfile atomically (tmp + os.replace), matching
    prism_cli._write_pid so the two writers never produce a torn file."""
    p = _pidfile_path()
    tmp = p.with_suffix(".pid.tmp")
    tmp.write_text(str(pid), encoding="utf-8")
    os.replace(tmp, p)


def _spawn_server() -> int:
    """Launch a fresh `python -m prism_service.main` server child and return
    its pid. Inherits the supervisor's env (the UI/MCP ports it was told to
    watch). Detached + breakaway so it outlives the launching shell, mirroring
    cli/prism_cli._spawn."""
    spawn_kwargs: dict = {}
    if sys.platform.startswith("win"):
        # CREATE_NO_WINDOW, not DETACHED_PROCESS — a console-less server makes
        # every console child (claude -p, git) flash a visible conhost window;
        # a hidden console is inherited silently (mirrors cli/prism_cli._spawn).
        _NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
        _NEW_GROUP = 0x00000200
        _BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        spawn_kwargs["creationflags"] = _NO_WINDOW | _NEW_GROUP | _BREAKAWAY
    else:
        spawn_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "prism_service.main"],
            stdin=subprocess.DEVNULL, close_fds=True, **spawn_kwargs,
        )
    except OSError as exc:
        if sys.platform.startswith("win"):
            # Breakaway forbidden by the job — the RESPAWNED server can only be
            # placed back inside the same job, so it inherits the wedge-recovery
            # role while remaining vulnerable to a kill-on-close reap. Warn so
            # the re-trap is visible (it used to be silent) rather than letting a
            # respawned-but-job-bound server look like a healthy recovery.
            _log(
                f"WARNING: CREATE_BREAKAWAY_FROM_JOB unavailable ({exc}); "
                f"respawned server runs INSIDE the job and may be reaped when "
                f"the launching session ends"
            )
            spawn_kwargs["creationflags"] = 0x08000000 | 0x00000200
            proc = subprocess.Popen(
                [sys.executable, "-m", "prism_service.main"],
                stdin=subprocess.DEVNULL, close_fds=True, **spawn_kwargs,
            )
        else:
            raise
    return proc.pid


def respawn_server() -> int:
    """Respawn the server child and update the pidfile to the NEW pid (old
    pid replaced). Returns the new pid so the caller can keep probing it."""
    new_pid = _spawn_server()
    try:
        _write_pid(new_pid)
    except OSError as exc:
        _log(f"could not update pidfile to new pid {new_pid}: {exc}")
    _log(f"respawned server (new pid {new_pid})")
    return new_pid


# ---------------------------------------------------------------------------
# Recovery decision — fire on the Nth consecutive hung probe (AC-2).
# Pure function of (state, alive) so the threshold logic is unit-testable
# without a live server.
# ---------------------------------------------------------------------------
def handle_probe_result(state: SupervisorState, alive: bool) -> bool:
    """Feed one probe result into the state machine. A healthy probe resets
    the streak. A hung probe increments it; on the Nth consecutive hang it
    fires recovery (force_kill the wedged pid + respawn) exactly ONCE and
    resets the streak to the fresh child. Returns True iff recovery fired."""
    if alive:
        state.consecutive_failures = 0
        return False

    # POST-RESPAWN GRACE: a freshly (re)spawned child is still cold-booting
    # (model load ~7-9s). Hung probes that arrive WHILE it is inside its
    # startup grace do NOT count toward a kill — a slow boot is never killed
    # mid-warmup. Re-applied on every respawn (the anchor is re-stamped below).
    if _in_startup_grace(state):
        return False

    # AUTO-UPDATE RESTART GRACE (GH #181): a fresh restart sentinel means the
    # worker is mid re-exec; its UI hang is expected, NOT a wedge. Defer the
    # competing kill+respawn that would otherwise stack a second daemon family.
    if _restart_in_progress():
        return False

    state.consecutive_failures += 1
    if state.consecutive_failures < failure_threshold():
        return False

    # Nth consecutive hang — recover.
    cap = max_restarts()
    if cap and state.restarts >= cap:
        _log(f"max_restarts={cap} reached; not respawning (server stays down)")
        state.consecutive_failures = 0
        return False

    _log(
        f"server pid {state.server_pid} hung for {state.consecutive_failures} "
        f"consecutive probes — SIGKILL + respawn"
    )
    force_kill(state.server_pid)
    state.server_pid = respawn_server()
    state.restarts += 1
    state.consecutive_failures = 0
    # Re-arm the startup grace for the FRESH child (per-respawn, not first-boot
    # only) so the cold respawn isn't killed mid-warmup on the next probes.
    state.mark_spawned()
    return True


# ---------------------------------------------------------------------------
# STRONG respawn trigger — the watched server PROCESS is GONE (task 3576dd3f).
# A crashed/killed server (process exited) is recovered IMMEDIATELY, without
# waiting out N HTTP-probe timeouts. Subject to the same startup grace and the
# crash-loop cap so a slow cold boot / a thrashing child are still protected.
# ---------------------------------------------------------------------------
def handle_liveness(state: SupervisorState, pid_alive: bool) -> bool:
    """If the watched server process is GONE, respawn a fresh server and rotate
    the pidfile to the new pid. A LIVE process is left to the HTTP-probe path
    (so an alive-but-slow daemon is never killed by this trigger). Returns True
    iff a respawn fired."""
    if pid_alive:
        return False
    if _in_startup_grace(state):
        return False
    # GH #181: don't respawn over a worker that is mid auto-update re-exec.
    if _restart_in_progress():
        return False
    cap = max_restarts()
    if cap and state.restarts >= cap:
        _log(f"max_restarts={cap} reached; not respawning the dead server")
        return False
    _log(f"server pid {state.server_pid} is GONE (process exited) — respawn")
    state.server_pid = respawn_server()
    state.restarts += 1
    state.consecutive_failures = 0
    state.mark_spawned()
    return True


def _sync_to_pidfile(state: SupervisorState) -> None:
    """Re-sync the watched pid to whatever the pidfile now names. The SERVER
    writes the pidfile with its OWN (real-listener) pid on boot/respawn, so a
    launcher pid handed to the supervisor is corrected to the true server pid
    here (pidfile truth); a deliberate manual restart that rewrote the pidfile
    is likewise tracked rather than fought."""
    fp = _read_pidfile_pid()
    if fp and fp != state.server_pid:
        _log(f"tracking server pid {fp} from pidfile (was {state.server_pid})")
        state.server_pid = fp
        state.consecutive_failures = 0


def supervise_once(state: SupervisorState, port: int | str) -> bool:
    """One supervise cycle. Returns False to ask the loop to EXIT (a deliberate
    `prism stop`), True to keep supervising.

    Order: (1) re-sync to the pidfile's truth; (2) PROCESS-GONE is the strong
    trigger — if the watched pid is dead, respawn immediately, UNLESS the
    pidfile no longer names it (operator stopped us: `prism stop` clears the
    pidfile before killing) in which case exit; (3) otherwise fall back to the
    HTTP wedge probe (graced + thresholded)."""
    _sync_to_pidfile(state)
    if state.server_pid and not server_process_alive(state.server_pid):
        if _read_pidfile_pid() != state.server_pid:
            _log("watched pid gone and pidfile cleared/replaced — treating as "
                 "an intentional stop; supervisor exiting")
            return False
        handle_liveness(state, pid_alive=False)
        return True
    handle_probe_result(state, probe_alive(port))
    return True


# ---------------------------------------------------------------------------
# The supervise loop — runs IN-LINE in this process's main (NOT a thread).
# ---------------------------------------------------------------------------
def supervise_loop(
    server_pid: int, port: int | str, *, fresh_boot: bool = False,
) -> None:
    """Probe the server's UI port on a cadence and recover it on a wedge.
    Loops forever in THIS process — no daemon thread is spawned here, by
    design: a thread would share the server's starved GIL and wedge too.

    `fresh_boot=True` (lead mode, where THIS process just spawned the server)
    stamps the startup-grace anchor so the initial cold boot is protected the
    same way every respawn is. In sibling mode the server cmd_start started is
    typically already warm, so no first-boot grace is stamped."""
    state = SupervisorState(server_pid=server_pid)
    # Always stamp the startup-grace anchor at loop entry — not just on a
    # fresh_boot spawn. It protects the boot window where the SERVER has not yet
    # rewritten the pidfile with its real pid (so the handed launcher pid may
    # read dead) AND a cold model-load boot (~7-9s), so neither is mistaken for
    # a crash before the first healthy probe. `fresh_boot` is retained for
    # callers but the grace is unconditional now (task 3576dd3f).
    state.mark_spawned()
    _log(
        f"watching server pid {server_pid} on :{port} "
        f"(interval={probe_interval_s()}s threshold={failure_threshold()} "
        f"grace={startup_grace_s()}s "
        f"bound~{probe_interval_s() * failure_threshold()}s)"
    )
    while True:
        if supervise_once(state, port) is False:
            _log("supervisor exiting (server intentionally stopped)")
            return
        time.sleep(probe_interval_s())


def run_supervisor(argv: list[str] | None = None) -> int:
    """Entry point for `python -m prism_service.services.supervisor [args]`.

    Two invocation shapes, both running the supervise loop IN-LINE (no thread):

    * LEAD MODE (no server-pid arg) — used by the systemd unit
      (deploy/prism.service). THIS process spawns the server as its OWN child
      and supervises it in the foreground, so the long-lived process systemd
      tracks as the unit main pid is the SUPERVISOR, not the server. The
      watched server's death/respawn is then invisible to systemd: a SIGKILL
      of the wedged child no longer tears down the cgroup, because the cgroup
      main pid (this supervisor) never exits.
    * SIBLING MODE (`<server_pid> <ui_port>`) — the proven cmd_start topology:
      cmd_start already started the server, and this supervises that pid.
    """
    args = argv if argv is not None else sys.argv[1:]
    if not is_enabled():
        _log("disabled (PRISM_SUPERVISOR=off)")
        return 0
    port = args[1] if len(args) >= 2 else os.environ.get("PRISM_UI_PORT", "7778")
    if len(args) >= 1 and str(args[0]).strip():
        # SIBLING MODE — supervise the pid cmd_start already started.
        server_pid = int(args[0])
        supervise_loop(server_pid, port)
        return 0
    # LEAD MODE — spawn the server as our own child and supervise it.
    server_pid = _spawn_server()
    try:
        _write_pid(server_pid)
    except OSError as exc:
        _log(f"could not write pidfile for lead-spawned pid {server_pid}: {exc}")
    _log(f"lead mode: spawned server child pid {server_pid} on :{port}")
    supervise_loop(server_pid, port, fresh_boot=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_supervisor())
