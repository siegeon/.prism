"""Per-task git workspace for the HONEST loop (task e825e00a).

Each task that enters the flow gets its own scratch git repo where the
worker commits REAL work — a real failing test at red, a real
implementation at green. The conductor verifier runs tier0 against THIS
repo (scoped to the recorded baseline), so a gate passes only on
genuinely committed, genuinely running tests — a fabricated trace with no
committed test yields zero tier0 claims and is refused.

Isolated on purpose: these repos live under data_dir/task_workspaces/ and
never touch the dev source checkout. Fold into a real worktree-off-main
model once the loop proves out.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from prism_service.data_dir import resolve_data_dir


def _root() -> Path:
    p = resolve_data_dir() / "task_workspaces"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _index_path() -> Path:
    return _root() / "index.json"


def _load_index() -> dict:
    p = _index_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save_index(idx: dict) -> None:
    _index_path().write_text(json.dumps(idx, indent=2))


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(ws), check=True,
                   capture_output=True, text=True)


def _rev(ws: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ws),
                          capture_output=True, text=True).stdout.strip()


def ensure_workspace(task_id: str) -> dict:
    """Create (idempotently) the task's scratch git repo with a baseline
    commit, and record {path, baseline} in the index. The baseline is what
    tier0 diffs against, so the worker's later commits are the scoped work."""
    idx = _load_index()
    rec = idx.get(task_id)
    if rec and Path(rec["path"]).exists():
        return rec
    ws = _root() / task_id
    ws.mkdir(parents=True, exist_ok=True)
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "worker@prism")
    _git(ws, "config", "user.name", "prism-worker")
    (ws / "pyproject.toml").write_text("[project]\nname = 'task'\nversion = '0'\n")
    (ws / "README.md").write_text(f"honest-loop workspace for task {task_id}\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "baseline")
    rec = {"task_id": task_id, "path": str(ws), "baseline": _rev(ws)}
    idx[task_id] = rec
    _save_index(idx)
    return rec


def workspace_for(task_id: str) -> Optional[dict]:
    rec = _load_index().get(task_id)
    if rec and Path(rec["path"]).exists():
        return rec
    return None
