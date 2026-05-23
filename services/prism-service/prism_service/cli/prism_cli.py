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


def _data_dir() -> Path:
    # Deferred import so `prism --help` doesn't load torch et al.
    from prism_service.data_dir import resolve_data_dir
    return resolve_data_dir()


def _pid_file() -> Path:
    return _data_dir() / "prism.pid"


def _log_file() -> Path:
    return _data_dir() / "prism.log"


def _is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        )
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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

    log = _log_file().open("ab", buffering=0)
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = 0x00000008 | 0x00000200  # DETACHED + NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, "-m", "prism_service.main"],
        env=env,
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    _pid_file().write_text(str(proc.pid), encoding="utf-8")
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
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"failed to stop pid {pid}: {e}", file=sys.stderr)
        return 1
    _pid_file().unlink(missing_ok=True)
    print(f"prism stopped (was pid {pid})")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    from prism_service.__version__ import PRISM_VERSION
    pid = _read_pid()
    alive = bool(pid) and _is_alive(pid)
    print(f"prism-service v{PRISM_VERSION}")
    print(f"  data dir: {_data_dir()}")
    print(f"  daemon:   {'running (pid ' + str(pid) + ')' if alive else 'stopped'}")
    if alive:
        ui = os.environ.get("PRISM_UI_PORT", "7778")
        mcp = os.environ.get("PRISM_MCP_PORT", "7777")
        print(f"  ui:       http://localhost:{ui}/")
        print(f"  mcp:      http://localhost:{mcp}/mcp/")
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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
