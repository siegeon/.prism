"""Per-task git WORKTREE for the HONEST loop (task e825e00a).

Each task that enters the flow gets its OWN git worktree of the PRISM
repo — a real branch off the task's base, an actual checkout of the
product source. The worker commits REAL work there (a real failing test
at red, a real implementation at green) and the conductor verifier runs
tier0 against THIS checkout, so a gate passes only on genuinely committed,
genuinely running tests. A fabricated trace with no committed test yields
zero tier0 claims and is refused.

This REPLACES the earlier empty-stub scratch repo (a bare pyproject+README
git init), which verified nothing real and — worse — let flow_start fall
back to the SHARED branch when it failed, cross-contaminating tasks. The
worktree model FAILS CLOSED: if a real worktree cannot be created, we
raise so flow_start refuses to start rather than sharing a branch.

Worktrees live under data_dir/task_workspaces/<task_id> and are registered
in the PRISM repo's worktree list; remove_workspace() unregisters them.
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


def _git_out(cwd: Path, *args: str) -> str:
    """Run git in `cwd`, raising RuntimeError (fail closed) on failure."""
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), check=True,
                           capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"git not available: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: "
            f"{(exc.stderr or exc.stdout or '').strip()}") from exc
    return r.stdout.strip()


def _prism_repo_root() -> Path:
    """Ascend from this module until a directory holding `.git` is found —
    the enclosing PRISM checkout. Raise (fail closed) if none is found so a
    packaged/wheel install without a source tree cannot silently degrade to
    a shared branch."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    raise RuntimeError(
        "no enclosing git checkout for the PRISM repo; refusing to create a "
        "fake workspace (fail closed)")


def ensure_workspace(task_id: str, repo_root: Optional[str] = None,
                     base_ref: Optional[str] = None) -> dict:
    """Create (idempotently) a real git worktree of the PRISM repo for this
    task and record {path, baseline, branch, repo_root} in the index.

    The worktree is a checkout of the PRODUCT source on a fresh branch off
    `base_ref` (default: the repo's current HEAD). `baseline` is that base
    commit, so the worker's later commits are the scoped work tier0 diffs
    against.

    FAIL CLOSED: any git failure (no repo, worktree add error) raises —
    callers must NOT fall back to a shared branch.
    """
    idx = _load_index()
    rec = idx.get(task_id)
    if rec and Path(rec["path"]).exists():
        return rec

    root = Path(repo_root) if repo_root else _prism_repo_root()
    if not (root / ".git").exists():
        raise RuntimeError(f"{root} is not a git checkout; refusing to create "
                           "a fake workspace (fail closed)")
    base = base_ref or _git_out(root, "rev-parse", "HEAD")

    ws = _root() / task_id
    branch = f"prism/ws/{task_id}"
    # Reap any orphaned registration from a prior partial run so a fresh id
    # is not blocked; harmless when there is nothing to prune.
    try:
        _git_out(root, "worktree", "prune")
    except RuntimeError:
        pass
    # git creates `ws`; adding -b makes the task's branch off `base`. A
    # failure here raises straight out of _git_out (fail closed).
    _git_out(root, "worktree", "add", "-b", branch, str(ws), base)
    _git_out(ws, "config", "user.email", "worker@prism")
    _git_out(ws, "config", "user.name", "prism-worker")

    rec = {"task_id": task_id, "path": str(ws), "baseline": _git_out(
        ws, "rev-parse", "HEAD"), "branch": branch, "repo_root": str(root)}
    idx[task_id] = rec
    _save_index(idx)
    return rec


def remove_workspace(task_id: str) -> dict:
    """Unregister and delete a task's worktree + its branch (best effort).
    Used on task teardown and by tests so the PRISM repo's worktree list
    stays clean."""
    idx = _load_index()
    rec = idx.pop(task_id, None)
    if rec is None:
        return {"ok": False, "reason": "no workspace for task"}
    root = Path(rec.get("repo_root") or "")
    if root.exists():
        for args in (("worktree", "remove", "--force", rec["path"]),
                     ("branch", "-D", rec.get("branch", "")),
                     ("worktree", "prune")):
            if not args[-1]:
                continue
            try:
                _git_out(root, *args)
            except RuntimeError:
                pass
    _save_index(idx)
    return {"ok": True, "removed": rec}


def workspace_for(task_id: str) -> Optional[dict]:
    rec = _load_index().get(task_id)
    if rec and Path(rec["path"]).exists():
        return rec
    return None
