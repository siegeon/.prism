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


def sweep_once() -> int:
    """Best-effort rmtree pass over every `.deleted`-marked project.
    PermissionError is logged but skipped — the next sweep retries.
    Returns the number of dirs actually removed."""
    if not PROJECTS_DIR.is_dir():
        return 0
    cleaned = 0
    for entry in PROJECTS_DIR.iterdir():
        if not entry.is_dir():
            continue
        if not is_deleted(entry):
            continue
        try:
            shutil.rmtree(entry)
            cleaned += 1
        except PermissionError as e:
            print(
                f"[trash] {entry.name}: still locked, will retry ({e})",
                file=sys.stderr, flush=True,
            )
        except FileNotFoundError:
            cleaned += 1
        except OSError as e:
            print(
                f"[trash] unexpected error on {entry.name}: {e}",
                file=sys.stderr, flush=True,
            )
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
