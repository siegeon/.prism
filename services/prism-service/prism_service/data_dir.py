"""Platform-aware data directory resolution for PRISM (v5.3.0+).

Order of precedence:

1. `PRISM_DATA_DIR` env var (absolute path)        — explicit override
2. `/data` if it already exists and is writable    — running inside the
                                                     docker image, keeps
                                                     v5.2.x layouts working
3. `%LOCALAPPDATA%\\prism` on Windows               — native install
4. `~/.prism` on macOS / Linux                      — native install

Returning `Path` instances (already-created) so callers can chain
`.mkdir(parents=True, exist_ok=True)` etc. without re-running platform
detection.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _windows_data_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "prism"
    return Path.home() / "AppData" / "Local" / "prism"


def _posix_data_root() -> Path:
    return Path.home() / ".prism"


def resolve_data_dir() -> Path:
    override = os.environ.get("PRISM_DATA_DIR")
    if override:
        p = Path(override).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    # /data only counts as the docker path on POSIX — on Windows it spuriously
    # resolves to <current-drive>:\data, which is not what we mean.
    if not sys.platform.startswith("win"):
        legacy_docker = Path("/data")
        if legacy_docker.exists() and os.access(legacy_docker, os.W_OK):
            return legacy_docker

    if sys.platform.startswith("win"):
        root = _windows_data_root()
    else:
        root = _posix_data_root()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def log_file() -> Path:
    """The daemon's rotating log file. Shared by the CLI (`prism logs`)
    and the in-process logging config (main._configure_logging) so the
    launcher and the daemon never disagree on the path (issue #66)."""
    return resolve_data_dir() / "prism.log"


def pid_file() -> Path:
    """The daemon pidfile. Shared by the CLI (start/stop/status) and the
    daemon's own atexit/signal cleanup so whoever writes it and whoever
    clears it agree on the path (issue #66)."""
    return resolve_data_dir() / "prism.pid"


LOG_MAX_BYTES = 5_000_000
LOG_BACKUPS = 5


def rotate_log_on_start(path: Path | None = None) -> None:
    """Roll prism.log -> prism.log.1 (.2 …) once at startup if it has
    grown past the cap, so the file is size-bounded and the PREVIOUS
    run's tail (incl. any crash trace) survives the restart instead of
    being lost (issue #66). Rotating only at startup — never mid-run —
    sidesteps the Windows "can't rename a file another handle has open"
    hazard a RotatingFileHandler would hit against the daemon's inherited
    stdout fd or a `prism logs --follow` reader. Lives here (not in the
    heavy main module) so the CLI can call it without importing FastAPI.
    """
    p = path or log_file()
    try:
        if not p.exists() or p.stat().st_size <= LOG_MAX_BYTES:
            return
        for i in range(LOG_BACKUPS - 1, 0, -1):
            src = p.with_name(f"{p.name}.{i}")
            if src.exists():
                src.replace(p.with_name(f"{p.name}.{i + 1}"))
        p.replace(p.with_name(f"{p.name}.1"))
    except OSError:
        pass  # best-effort; never block startup on log rotation


def resolve_claude_home() -> Path:
    """Where PRISM should find the user's Claude Code config (~/.claude).

    Honors `CLAUDE_CONFIG_DIR` (the canonical env var Claude Code itself reads)
    so a docker bind-mount or sandboxed user can still point us at the right
    creds dir.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"
