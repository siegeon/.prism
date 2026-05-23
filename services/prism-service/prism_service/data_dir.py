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
