"""Live task state for the plan steps (task 70d14f84).

The draft_story / verify_plan steps read what is TRUE NOW -- the fetched
origin/main sha, the task worktree head, and the child list across all
statuses with LIVE gate readiness -- never a stored snapshot. Non-policy
helper: conductor_flow._job attaches compute() and appends render().
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional


def main_checkout() -> str:
    """The daemon's main checkout: the git root that contains this package."""
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / ".git").exists():
            return str(p)
    return str(here.parents[3])


def _git(cwd, *args, timeout: float = 20.0) -> Optional[str]:
    if not cwd:
        return None
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout)
    except Exception:  # noqa: BLE001 - a live read never crashes a job
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def compute(task, task_svc, readiness_fn: Callable[[str], dict],
            main_checkout: str, worktree_path: Optional[str],
            fetch_timeout_s: float = 1.5) -> dict:
    fetched = _git(main_checkout, "fetch", "-q", "origin", "main",
                   timeout=fetch_timeout_s) is not None
    origin_sha = _git(main_checkout, "rev-parse", "origin/main")
    head = _git(worktree_path, "rev-parse", "HEAD") if worktree_path else None
    behind = None
    if origin_sha and head and origin_sha != head:
        anc = _git(main_checkout, "merge-base", "--is-ancestor",
                   origin_sha, head)
        if anc is None:
            anc = _git(worktree_path, "merge-base", "--is-ancestor",
                       origin_sha, head)
        behind = anc is None
    elif origin_sha and head:
        behind = False
    children = []
    for c in task_svc.list(parent_id=task.id):
        try:
            readiness = readiness_fn(c.id)
        except Exception as exc:  # noqa: BLE001
            readiness = {"error": str(exc)}
        children.append({"id": c.id, "status": c.status,
                         "title": c.title, "readiness": readiness})
    return {
        "origin_main_sha": origin_sha,
        "origin_main_fetched": fetched,
        "worktree_path": worktree_path,
        "worktree_head": head,
        "worktree_behind_origin_main": behind,
        "child_count": len(children),
        "children": children,
    }


def render(ls: dict) -> str:
    sha = ls.get("origin_main_sha") or "unknown"
    tag = "" if ls.get("origin_main_fetched") else " (UNFETCHED: local ref)"
    n = ls.get("child_count", 0)
    lines = [
        "LIVE STATE (read now, not a stored snapshot):",
        f"- origin/main {sha}{tag}",
        f"- worktree HEAD {ls.get('worktree_head') or 'none'}"
        f" (behind origin/main: {ls.get('worktree_behind_origin_main')})",
        f"- children: {n}",
    ]
    for c in ls.get("children") or []:
        r = c.get("readiness") or {}
        status = r.get("status") or r.get("state") or "-"
        lines.append(f"  - {c['id'][:8]} [{c['status']}] {c.get('title', '')}"
                     f" -- readiness: {status}")
    lines.append(
        f"Cite `origin/main {sha}` and `children: {n}` in the artifact. "
        "Do not cite a worktree HEAD or a stored gate_reason as current.")
    return "\n".join(lines)
