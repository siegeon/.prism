"""`prism` CLI — top-level entry point for the v5.3.0 pip distribution.

Subcommands:

  prism start [--daemon] [--ui-port N] [--mcp-port N]
        Boot the FastAPI + MCP service. Foreground unless --daemon is set;
        --daemon detaches and writes a pidfile under the data dir.

  prism stop
        SIGTERM the daemonized service via its pidfile.

  prism status
        Show: data-dir path, version, whether a daemon is alive, ports,
        UI URL.

  prism logs [--follow]
        Tail the daemon's stdout/stderr capture file. --follow streams.

  prism update
        `pip install --upgrade prism-service`. Convenience wrapper so
        users with pipx installs don't have to remember the exact command.

  prism version
        Print version + version notes.

This module deliberately stays small — heavy imports (FastAPI, torch, etc.)
are deferred into the subcommand bodies so `prism --help` is instant.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

# Windows defaults stdout to cp1252; force UTF-8 so the em-dashes and
# arrows in version notes don't UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# GH #155 — pin native-math thread pools to 1 at the VERY TOP of the CLI
# boot, BEFORE any subcommand body's deferred numpy/model2vec import (the
# BLAS backends read these vars once at load). Operator-set values are left
# untouched. This is the PREVENT layer of the silent-all-threads-wedge fix.
from prism_service.thread_limits import apply_thread_limits
apply_thread_limits()


def _data_dir() -> Path:
    # Deferred import so `prism --help` doesn't load torch et al.
    from prism_service.data_dir import resolve_data_dir
    return resolve_data_dir()


def _pid_file() -> Path:
    from prism_service.data_dir import pid_file
    return pid_file()


def _log_file() -> Path:
    from prism_service.data_dir import log_file
    return log_file()


def _write_pid(pid: int) -> None:
    """Publish the pidfile atomically (write tmp + os.replace) so a crash
    mid-write or a concurrent reader never sees a torn pidfile (#66)."""
    p = _pid_file()
    tmp = p.with_suffix(".pid.tmp")
    tmp.write_text(str(pid), encoding="utf-8")
    os.replace(tmp, p)


def _is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        # CSV + exact PID-column compare — the old `str(pid) in stdout`
        # substring test false-positives (123 matches a row with 1234,
        # or the pid appearing in an image name / memory column) (#66).
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


def _daemon_http_alive(port: int | str) -> bool:
    """Probe the daemon's own /api/version. Used as a lightweight identity
    check so status/stop don't trust a recycled PID (#66). 127.0.0.1 only
    (the WSL wslrelay trap, #64). No new dependency — stdlib urllib."""
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/version", timeout=0.5,
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _port_owner_pid(port: int | str):
    """PID owning the LISTEN socket on `port`, else None (GH #181). Thin,
    monkeypatchable seam over services.split_brain.port_owner_pid; deferred
    import keeps `prism --help` light."""
    from prism_service.services.split_brain import port_owner_pid
    return port_owner_pid(port)


def _read_pid() -> int:
    p = _pid_file()
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def cmd_start(args: argparse.Namespace) -> int:
    env = os.environ.copy()
    if args.ui_port:
        env["PRISM_UI_PORT"] = str(args.ui_port)
    if args.mcp_port:
        env["PRISM_MCP_PORT"] = str(args.mcp_port)

    if not args.daemon:
        # Foreground — defer to main.py's `if __name__ == "__main__"` path
        # by re-executing as a module. Lets users Ctrl-C cleanly.
        os.execvpe(sys.executable, [sys.executable, "-m", "prism_service.main"], env)
        return 0  # never reached

    existing = _read_pid()
    if existing and _is_alive(existing):
        print(f"prism is already running (pid {existing})", file=sys.stderr)
        return 1
    if existing:
        # Confirmed-dead pid lingering on disk — clear it proactively so
        # status/stop never report a stale daemon as running (#66). This
        # used to only happen on an explicit `prism stop`.
        print(f"removing stale pidfile (pid {existing} not alive)", file=sys.stderr)
        _pid_file().unlink(missing_ok=True)

    # PORT-LEVEL GUARD (GH #181): the pidfile check above MISSES a live daemon
    # when the pidfile is absent/stale/mid-rotation — exactly the auto-update
    # window in which a second `prism start` stacked a second family and caused
    # the split-brain. Refuse to start if a daemon already answers on the UI
    # port, or if anything holds the MCP port, regardless of pidfile state.
    ui_guard = str(args.ui_port or os.environ.get("PRISM_UI_PORT", "7778"))
    mcp_guard = str(args.mcp_port or os.environ.get("PRISM_MCP_PORT", "7777"))
    if _daemon_http_alive(ui_guard):
        print(f"prism already running (UI :{ui_guard} is answering) — refusing "
              f"to start a second daemon", file=sys.stderr)
        return 1
    _mcp_owner = _port_owner_pid(mcp_guard)
    if _mcp_owner:
        print(f"MCP port :{mcp_guard} already held by pid {_mcp_owner} — "
              f"refusing to stack a second family (possible in-flight restart)",
              file=sys.stderr)
        return 1

    # Rotate the prior run's log BEFORE opening the append handle so the
    # file is size-bounded and the last run's tail (incl. any crash
    # trace) is preserved as prism.log.1 rather than growing unbounded
    # or being lost (#66). Rotating here — before the handle exists —
    # avoids the Windows rename-while-open hazard.
    from prism_service.data_dir import rotate_log_on_start
    rotate_log_on_start(_log_file())
    # Append (never truncate) so a restart keeps the prior run's tail.
    # The child's stdout+stderr (every print() + uvicorn's default stderr
    # logging) land here; the daemon also installs faulthandler/excepthook
    # against this same stream (main._configure_logging) for crash traces.
    log = _log_file().open("ab")
    spawn_kwargs: dict = {}
    if sys.platform.startswith("win"):
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP alone is NOT enough
        # when the parent shell runs inside a Windows job object with
        # kill-on-close (agent harnesses, CI runners, Terminal profiles):
        # the detached child still belongs to the job and is reaped the
        # moment the job handle closes — the daemon dies with no traceback
        # the instant the launching session ends. CREATE_BREAKAWAY_FROM_JOB
        # escapes the job. It raises if the job forbids breakaway, so fall
        # back to the old flags rather than failing the launch.
        # CREATE_NO_WINDOW (not DETACHED_PROCESS): a DETACHED daemon has NO
        # console, so every console-subprocess it spawns (claude -p, git,
        # pytest) allocates a fresh VISIBLE conhost window — worker activity
        # flashes terminal windows at the user. NO_WINDOW gives the daemon a
        # hidden console that all children inherit: same detachment from the
        # launching terminal, zero window spam.
        _NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
        _NEW_GROUP = 0x00000200
        _BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        spawn_kwargs["creationflags"] = _NO_WINDOW | _NEW_GROUP | _BREAKAWAY
    else:
        spawn_kwargs["start_new_session"] = True

    def _spawn() -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "prism_service.main"],
            env=env,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            **spawn_kwargs,
        )

    try:
        proc = _spawn()
    except OSError as exc:
        if sys.platform.startswith("win"):
            # The launching shell's job object forbids CREATE_BREAKAWAY_FROM_JOB
            # (no JOB_OBJECT_LIMIT_BREAKAWAY_OK). The only fallback is to retry
            # WITHOUT breakaway — which leaves the daemon INSIDE that job, so the
            # OS reaps it (no traceback, no Python exception) the instant the
            # launching session's job handle closes. This is the silent "dev
            # daemon dies every few minutes under an agent/CI harness" failure;
            # it used to fall back here invisibly. Warn loudly so it is
            # diagnosable, then re-trap as a last resort. For a DURABLE daemon,
            # launch from a shell whose job permits breakaway or from outside a
            # kill-on-close job.
            print(
                f"WARNING: CREATE_BREAKAWAY_FROM_JOB unavailable ({exc}); the "
                f"daemon will run INSIDE the launching job and be KILLED with no "
                f"traceback when this session ends. Launch from a shell whose "
                f"job permits breakaway (or outside a kill-on-close job) for a "
                f"durable daemon.",
                file=sys.stderr,
            )
            spawn_kwargs["creationflags"] = 0x08000000 | 0x00000200
            proc = _spawn()
        else:
            raise
    _write_pid(proc.pid)

    # GH #162 — launch the OUT-OF-PROCESS supervisor as its OWN child
    # (python -m prism_service.services.supervisor <server_pid> <ui_port>).
    # It HTTP-probes the server's UI port off the server's contended GIL
    # path and, after N consecutive timeouts, SIGKILLs the wedged server and
    # respawns it — turning the silent all-threads futex wedge into a ~5s
    # self-restart. A SEPARATE PROCESS, never an in-process thread (a thread
    # shares the same starved GIL and wedges identically). Best-effort: a
    # supervisor spawn failure must never fail the daemon launch.
    ui_port = str(args.ui_port or os.environ.get("PRISM_UI_PORT", "7778"))
    if os.environ.get("PRISM_SUPERVISOR", "").lower() not in ("0", "off", "false", "no"):
        try:
            subprocess.Popen(
                [sys.executable, "-m", "prism_service.services.supervisor",
                 str(proc.pid), ui_port],
                env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                close_fds=True, **spawn_kwargs,
            )
        except OSError as exc:
            print(f"warning: supervisor not started: {exc}", file=sys.stderr)

    print(f"prism started (pid {proc.pid})")
    print(f"  data dir: {_data_dir()}")
    print(f"  ui:       http://localhost:{args.ui_port or os.environ.get('PRISM_UI_PORT', '7778')}/")
    print(f"  mcp:      http://localhost:{args.mcp_port or os.environ.get('PRISM_MCP_PORT', '7777')}/mcp/")
    print(f"  logs:     prism logs --follow")
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    pid = _read_pid()
    if not pid:
        print("no pidfile — is prism running?", file=sys.stderr)
        return 1
    if not _is_alive(pid):
        print(f"stale pidfile (pid {pid} not alive); cleaning up", file=sys.stderr)
        _pid_file().unlink(missing_ok=True)
        return 0
    # PID is alive — but is it actually prism, or a recycled PID? If the
    # daemon's /api/version doesn't answer, refuse to kill an innocent
    # process; just clear the pidfile (#66).
    ui = os.environ.get("PRISM_UI_PORT", "7778")
    if not _daemon_http_alive(ui):
        print(
            f"pid {pid} is alive but not responding as prism on :{ui} — "
            f"refusing to kill (possible recycled PID); clearing pidfile only",
            file=sys.stderr,
        )
        _pid_file().unlink(missing_ok=True)
        return 0
    # Clear the pidfile BEFORE the kill. The out-of-process supervisor is a
    # SEPARATE process the server's /T tree-kill never reaches, so it must be
    # told this is a DELIBERATE stop — not a crash to respawn. supervise_once
    # reads "watched pid dead AND pidfile no longer names it" as an intentional
    # stop and exits; clearing first closes the race where it could respawn in
    # the window between the kill and the unlink (task 3576dd3f).
    _pid_file().unlink(missing_ok=True)
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"failed to stop pid {pid}: {e}", file=sys.stderr)
        return 1
    # ORPHAN SWEEP (GH #181 fix #4): a split-brain orphan is a SEPARATE family
    # squatting the OTHER port — the pidfile never named it, so the stop above
    # didn't reach it, and (as observed) it IGNORED SIGTERM. Reap any remaining
    # port-squatter with an escalating SIGTERM->SIGKILL so a graceful stop never
    # leaves a zombie holding a port.
    try:
        mcp = os.environ.get("PRISM_MCP_PORT", "7777")
        from prism_service.services import split_brain
        rep = split_brain.detect_split_brain(
            ui, mcp, pidfile_pid=pid, owner=_port_owner_pid)
        if not rep.healthy and rep.orphan_pid and rep.orphan_pid != pid:
            split_brain.heal_split_brain(rep)
            print(f"reaped split-brain orphan pid {rep.orphan_pid} "
                  f"(SIGTERM->SIGKILL)", file=sys.stderr)
    except Exception:
        pass
    print(f"prism stopped (was pid {pid})")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    from prism_service.__version__ import PRISM_VERSION
    pid = _read_pid()
    ui = os.environ.get("PRISM_UI_PORT", "7778")
    mcp = os.environ.get("PRISM_MCP_PORT", "7777")
    pid_alive = bool(pid) and _is_alive(pid)
    # "running" requires BOTH a live PID AND the daemon answering on its
    # port — a live-but-unresponsive PID is a stale/recycled pidfile, not
    # a running prism (#66).
    responding = pid_alive and _daemon_http_alive(ui)
    print(f"prism-service v{PRISM_VERSION}")
    print(f"  data dir: {_data_dir()}")
    if responding:
        status = f"running (pid {pid})"
    elif pid_alive:
        status = f"stale pidfile (pid {pid} alive but not responding on :{ui})"
    elif pid:
        status = f"stopped (stale pidfile pid {pid})"
    else:
        status = "stopped"
    print(f"  daemon:   {status}")
    if responding:
        print(f"  ui:       http://localhost:{ui}/")
        print(f"  mcp:      http://localhost:{mcp}/mcp/")

    # SPLIT-BRAIN SELF-CHECK (GH #181): two PIDs each owning ONE port is the
    # silent failure the version/HTTP checks above MISS — UI reports the new
    # version and looks green while MCP is stranded on the orphaned old daemon.
    # Detect it (one pid owning both = healthy) and self-heal by reaping the
    # orphan, surfacing UNHEALTHY (non-zero exit) instead of a false green.
    from prism_service.services import split_brain
    report = split_brain.detect_split_brain(
        ui, mcp, pidfile_pid=pid, owner=_port_owner_pid)
    if not report.healthy:
        print(f"  health:   UNHEALTHY — {report.reason}")
        reaped = split_brain.heal_split_brain(report)
        if reaped:
            print(f"  self-heal: reaped orphan pid {reaped} (SIGTERM->SIGKILL); "
                  f"re-run 'prism status' to confirm a single owner")
        return 1
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    log = _log_file()
    if not log.exists():
        print(f"no log file at {log}", file=sys.stderr)
        return 1
    if not args.follow:
        sys.stdout.write(log.read_text(encoding="utf-8", errors="replace"))
        return 0
    # poor-man's tail -f, cross-platform
    import time
    with log.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(0, 2)
        try:
            while True:
                line = fh.readline()
                if not line:
                    time.sleep(0.25)
                    continue
                sys.stdout.write(line)
                sys.stdout.flush()
        except KeyboardInterrupt:
            return 0


def cmd_update(_args: argparse.Namespace) -> int:
    rc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "prism-service"],
        check=False,
    ).returncode
    return rc


def cmd_version(_args: argparse.Namespace) -> int:
    from prism_service.__version__ import PRISM_VERSION, PRISM_VERSION_NOTES
    print(f"prism-service {PRISM_VERSION}")
    print()
    print(PRISM_VERSION_NOTES)
    return 0


def cmd_principles_seed(args: argparse.Namespace) -> int:
    """Seed the active project's architecture principles so the conductor's
    plan_gate is satisfiable (issue #171). In-process — resolves the project
    context directly (no running daemon needed)."""
    from prism_service.config import DEFAULT_PROJECT
    from prism_service.project_context import get_project
    from prism_service.services.arc_governance import seed_default_principles

    pid = getattr(args, "project", None) or DEFAULT_PROJECT
    ctx = get_project(pid)
    stored = seed_default_principles(ctx.memory_svc)
    ids = ", ".join(getattr(e, "name", "") for e in stored)
    print(f"seeded {len(stored)} architecture principle(s) into project "
          f"'{pid}': {ids}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prism", description="PRISM service CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="Start the PRISM service (foreground by default)")
    s.add_argument("--daemon", action="store_true", help="Detach into the background")
    s.add_argument("--ui-port", type=int, default=None)
    s.add_argument("--mcp-port", type=int, default=None)
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("stop", help="Stop the backgrounded PRISM service")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("status", help="Show service status")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("logs", help="Show / tail the daemon log file")
    s.add_argument("--follow", "-f", action="store_true")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("update", help="pip install --upgrade prism-service")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("version", help="Print version + notes")
    s.set_defaults(func=cmd_version)

    s = sub.add_parser(
        "principles", help="Architecture-principles governance (issue #171)")
    psub = s.add_subparsers(dest="pcmd", required=True)
    ps = psub.add_parser(
        "seed", help="Seed default architecture principles for a project")
    ps.add_argument("--project", default=None,
                    help="Project id (default: the implicit 'default' project)")
    ps.set_defaults(func=cmd_principles_seed)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
