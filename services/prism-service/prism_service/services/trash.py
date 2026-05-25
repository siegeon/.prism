"""Lock-tolerant project deletion (v5.3.0).

Synchronous `shutil.rmtree(project_dir)` and even `os.rename()` hit
`[WinError 5] Access is denied` on Windows when the drift/governance/
quality/drainer timer threads still hold SQLite connections open
against the project's .db files (the timers cache per-project Brain
instances on their own connections, by design — issue #38 — so a long
reindex can't park MCP workers).

This module uses a soft-delete marker file instead of moving anything:

  1. `delete_project()` writes `<pdir>/.deleted` and returns success.
  2. `list_projects()` filters out marked dirs so the SPA stops showing
     them immediately.
  3. The background `sweep_once()` loop tries `shutil.rmtree` on every
     marked dir. The first attempt usually fails on file lock; subsequent
     passes succeed once the next timer iteration drops the Brain ref
     (the timer skips projects not in `get_all_projects()`).

Public surface:
  * `MARKER_NAME` — the well-known file inside a project dir.
  * `mark_deleted(project_dir)` — write the marker.
  * `is_deleted(project_dir)` — test for it.
  * `sweep_once()` — best-effort rmtree pass; logs but never raises,
    returns the count actually removed.
  * `start_trash_sweeper()` — daemon-loop helper; honors
    `PRISM_TRASH_SWEEP_INTERVAL` (seconds, default 30; 0 disables).
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from prism_service.config import PROJECTS_DIR

MARKER_NAME = ".deleted"


def mark_deleted(project_dir: Path) -> Path:
    """Write a `.deleted` marker inside `project_dir` so it stops being
    listed. Returns the marker path. Idempotent."""
    if not project_dir.is_dir():
        raise FileNotFoundError(project_dir)
    marker = project_dir / MARKER_NAME
    marker.write_text(str(int(time.time())), encoding="utf-8")
    return marker


def is_deleted(project_dir: Path) -> bool:
    return (project_dir / MARKER_NAME).is_file()


# Track sweep attempts per entry so we log the first "still locked" warning
# loud, then stay quiet until either the entry is cleaned or we hit
# _LOUD_RETRY_AT (at which point we log once more — the user has probably
# left a tool open and should know). Cleared when the entry is gone.
_LOCK_ATTEMPTS: dict[str, int] = {}
_LOUD_RETRY_AT = 120  # ~1h at the default 30s interval


def sweep_once() -> int:
    """Best-effort rmtree pass over every `.deleted`-marked project.
    PermissionError is logged once per entry — subsequent passes stay
    quiet so the log doesn't spam every 30s. Returns the count cleaned."""
    if not PROJECTS_DIR.is_dir():
        return 0
    cleaned = 0
    live_names: set[str] = set()
    for entry in PROJECTS_DIR.iterdir():
        if not entry.is_dir():
            continue
        if not is_deleted(entry):
            continue
        live_names.add(entry.name)
        try:
            shutil.rmtree(entry)
            cleaned += 1
            if _LOCK_ATTEMPTS.pop(entry.name, 0):
                print(
                    f"[trash] {entry.name}: cleaned after retry",
                    file=sys.stderr, flush=True,
                )
        except PermissionError as e:
            n = _LOCK_ATTEMPTS.get(entry.name, 0) + 1
            _LOCK_ATTEMPTS[entry.name] = n
            if n == 1 or n == _LOUD_RETRY_AT:
                suffix = "" if n == 1 else f" (still after {n} sweeps — close any tool holding this dir)"
                print(
                    f"[trash] {entry.name}: still locked, will retry ({e}){suffix}",
                    file=sys.stderr, flush=True,
                )
        except FileNotFoundError:
            cleaned += 1
            _LOCK_ATTEMPTS.pop(entry.name, None)
        except OSError as e:
            print(
                f"[trash] unexpected error on {entry.name}: {e}",
                file=sys.stderr, flush=True,
            )
    # Drop tracking for entries no longer present (cleaned by another path,
    # or the marker was removed externally).
    for stale in [k for k in _LOCK_ATTEMPTS if k not in live_names]:
        _LOCK_ATTEMPTS.pop(stale, None)
    return cleaned


def start_trash_sweeper() -> None:
    """Daemon-loop driver. Run inside `threading.Thread(daemon=True)`."""
    interval = int(os.environ.get("PRISM_TRASH_SWEEP_INTERVAL", "30"))
    if interval <= 0:
        print("[trash] sweeper disabled (PRISM_TRASH_SWEEP_INTERVAL=0)",
              file=sys.stderr, flush=True)
        return
    print(f"[trash] sweeper running every {interval}s",
          file=sys.stderr, flush=True)
    while True:
        try:
            n = sweep_once()
            if n:
                print(f"[trash] cleaned {n} entry(ies)",
                      file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[trash] sweeper error: {e}",
                  file=sys.stderr, flush=True)
        time.sleep(interval)
