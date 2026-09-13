"""task_reaper.sweep_worktrees must never reap a task's own worktree while
the task itself is still active (ops incident 2026-09-13, task a65c66e5).

Companion to the ensure_workspace recovery fix in
test_ensure_workspace_recreates_missing_directory.py. The sweep's
dirty/age/ancestor-of-main checks say nothing about whether a task is
actually DONE: a clean worktree whose branch happens to be level with
(an ancestor of) origin/main -- the normal shape of a task sitting at
plan_gate/verify_plan, having committed nothing yet beyond its baseline --
was reaped purely by age. That is exactly what happened live: task
a65c66e5 was `in_progress` at `verify_plan`, its worktree was clean, and
its branch was a pure ancestor of origin/main (zero unique commits) --
the sweep removed it anyway, and the task was left permanently refusing
to start ("workspace unavailable, refusing to start (fail closed)").

The fix: for a worktree whose branch matches `prism/ws/<task_id>`, look up
that task's live status across every project. A task that is anything
other than done/cancelled survives the sweep regardless of age or
cleanliness. No matching task row (an orphaned branch, or a task_id
unknown to any project) keeps the OLD task-agnostic behavior unchanged --
see test_the_sweep_reaps_a_task_worktree_too_when_orphaned in
test_worktree_sweep_reaps_non_task_worktrees.py, which this must not
break.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from prism_service import project_context
from prism_service.services import task_reaper, task_workspace


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {r.stderr}")
    return r.stdout.strip()


def _age(path: Path, hours: float) -> None:
    import os
    old = time.time() - hours * 3600.0
    os.utime(path, (old, old))


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "base")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(task_workspace, "resolve_data_dir",
                        lambda: tmp_path / "data")
    return root


def _make_task_worktree(repo: Path, project: str, status: str) -> Path:
    """A real task row (in `project`) plus its OWN git worktree: clean,
    level with origin/main, and old enough to sweep -- the exact shape
    task a65c66e5 had at the moment it was wrongly reaped."""
    ctx = project_context.get_project(project)
    task = ctx.task_svc.create(title="pinned reap-guard task",
                               workflow="implement")
    ctx.task_svc.update(task.id, status=status)
    rec = task_workspace.ensure_workspace(task.id, repo_root=str(repo))
    ws = Path(rec["path"])
    _age(ws, 48.0)
    return ws


def test_a_worktree_of_an_in_progress_task_is_never_reaped(
        repo: Path) -> None:
    ws = _make_task_worktree(repo, "default", "in_progress")

    result = task_reaper.sweep_worktrees(repo_root=str(repo))

    item = {i["path"]: i for i in result["items"]}[str(ws)]
    assert item["reaped"] is False, item["reason"]
    assert ws.exists()
    assert "in_progress" in item["reason"].lower()


def test_a_worktree_of_a_blocked_task_is_never_reaped(repo: Path) -> None:
    ws = _make_task_worktree(repo, "default", "blocked")

    result = task_reaper.sweep_worktrees(repo_root=str(repo))

    item = {i["path"]: i for i in result["items"]}[str(ws)]
    assert item["reaped"] is False, item["reason"]
    assert ws.exists()


def test_a_worktree_of_a_done_task_is_still_reaped_by_the_sweep(
        repo: Path) -> None:
    """The guard is a NEW refusal reason, never a blanket keep-everything
    -- a genuinely finished task's worktree is still fair game."""
    ws = _make_task_worktree(repo, "default", "done")

    result = task_reaper.sweep_worktrees(repo_root=str(repo))

    item = {i["path"]: i for i in result["items"]}[str(ws)]
    assert item["reaped"] is True, item["reason"]
    assert not ws.exists()
