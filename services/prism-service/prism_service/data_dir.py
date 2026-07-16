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
import time
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


def prototype_dir() -> Path:
    """Directory holding per-task prototype HTML — clickable MOCK-data mock
    UIs the /prototype workflow generates, served by PRISM and iframed on the
    task detail Plan card so prototypes are viewable IN-APP, not on an
    external port."""
    p = resolve_data_dir() / "prototypes"
    p.mkdir(parents=True, exist_ok=True)
    return p


def prototype_file(task_id: str) -> Path:
    """Path to a task's prototype HTML. Callers MUST pass an already-validated
    task id (the API route restricts it to [A-Za-z0-9_-]) so a crafted id can't
    traverse out of the prototypes dir."""
    return prototype_dir() / f"{task_id}.html"


def evidence_dir(task_id: str) -> Path:
    """Directory holding a task's evidence files — the screenshots/artifacts a
    drive cites in its proof, served by PRISM and rendered inline on the task
    detail page so the gate approver SEES what the proof cites (evidence
    viewable IN PRISM, like prototypes — never an external host). Callers MUST
    pass an already-validated task id (the API route restricts it to
    [A-Za-z0-9_-]) so a crafted id can't traverse out of the evidence dir."""
    p = resolve_data_dir() / "evidence" / task_id
    p.mkdir(parents=True, exist_ok=True)
    return p


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


# Restart-coordination sentinel (GH #181). The auto-updater writes it BEFORE
# draining for an os.execv re-exec; the re-exec'd server clears it on a healthy
# boot. While the sentinel is FRESH the out-of-process supervisor treats the
# worker as in-grace and skips its competing kill+respawn — that competing
# recovery (UI hung during the restart drain/cold-boot) is what spawned a
# second daemon family and the split-brain. The TTL is the NFR-2 staleness
# bound: a crash mid-restart leaves a sentinel behind, and a STALE one must NOT
# disable supervisor recovery forever, so freshness is always re-checked.
RESTART_SENTINEL_TTL_S = 120.0


def restart_sentinel() -> Path:
    """Path of the restart-in-progress sentinel (shared by auto_updater,
    main, and supervisor so all three agree on the file)."""
    return resolve_data_dir() / "prism.restarting"


def write_restart_sentinel(pid: int | None = None) -> None:
    """Mark a restart in progress (best-effort). Records the pid for diagnostics."""
    p = restart_sentinel()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(pid if pid is not None else os.getpid()), encoding="utf-8")
    except OSError:
        pass


def clear_restart_sentinel() -> None:
    """Clear the restart sentinel (called by the re-exec'd server on healthy boot)."""
    try:
        restart_sentinel().unlink(missing_ok=True)
    except OSError:
        pass


def restart_in_progress(ttl_s: float = RESTART_SENTINEL_TTL_S) -> bool:
    """True iff a FRESH restart sentinel exists (younger than ttl_s). A stale
    sentinel (crash mid-restart) returns False so recovery is never disabled
    forever (NFR-2). Absent file => False."""
    try:
        # Clamp to >=0: a just-written file's st_mtime can read marginally
        # AHEAD of time.time() (filesystem mtime resolution / clock skew),
        # which would make a FRESH sentinel look negative-aged. Clamping keeps
        # a fresh file fresh while ttl_s=0 still reads as stale (0 < 0 is False).
        age = max(0.0, time.time() - restart_sentinel().stat().st_mtime)
    except OSError:
        return False
    return age < max(0.0, ttl_s)


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
