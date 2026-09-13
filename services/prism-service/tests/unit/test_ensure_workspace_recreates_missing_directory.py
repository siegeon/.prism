"""ensure_workspace recovers a task whose worktree DIRECTORY is gone but
whose own `prism/ws/<task_id>` branch survives (task a65c66e5, ops
incident 2026-09-13).

A reap pass (task_reaper.sweep_worktrees) removed a clean worktree that
was level with origin/main while its task was still `in_progress` at
plan_gate. The workspace index STILL held the stale record (path no
longer exists), and the task's branch was NOT deleted (`git branch -D`
never ran, or ran and was recreated by a stale re-drive attempt -- either
way, live: `prism/ws/a65c66e5-...` existed with zero commits ahead of
origin/main while its directory was gone).

Before this fix, `ensure_workspace`'s "no usable existing record" branch
always ran `git worktree add -b <branch> <path> <base>` -- `-b` demands
the branch NOT already exist, so it raised `fatal: A branch named
'prism/ws/<id>' already exists.` on every call, forever. `flow_start`
(api/conductor_flow.py) turned that into a permanent
"workspace unavailable, refusing to start (fail closed)" refusal, and
task_runner/resume_actuator logged it every tick with no way to recover.

This suite pins the fix: when the recorded (or conventional) path is
missing, `ensure_workspace` checks out the EXISTING branch into a fresh
worktree at that path (recovering it and any commits it carries) instead
of trying to create the branch anew; only when the branch does not exist
anywhere does it fall back to a fresh branch off `base_ref`/HEAD.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from prism_service.services import task_workspace


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {r.stderr}")
    return r.stdout.strip()


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
    monkeypatch.setattr(task_workspace, "resolve_data_dir",
                        lambda: tmp_path / "data")
    return root


def test_ac1_recovers_a_worktree_removed_out_from_under_a_live_branch(
        repo: Path) -> None:
    """AC-1: the exact live shape -- worktree dir removed by something
    else (a reap sweep, an operator), branch untouched, index record
    stale. ensure_workspace must recreate the worktree at the SAME path,
    checking out the SAME branch, never raising "branch already exists"."""
    task_id = "a65c66e5-b8a7-44b4-a223-f1342cfaaa14"
    rec = task_workspace.ensure_workspace(task_id, repo_root=str(repo))
    ws = Path(rec["path"])
    branch = rec["branch"]
    assert ws.exists()

    # Simulate the reap: rip out the worktree directory + git's own
    # registration WITHOUT deleting the branch (exactly what the live
    # incident left behind) and WITHOUT touching the index (also observed
    # live -- the stale record survived).
    _git(repo, "worktree", "remove", "--force", str(ws))
    assert not ws.exists()
    assert branch in _git(repo, "branch", "--format=%(refname:short)").splitlines()

    recovered = task_workspace.ensure_workspace(task_id, repo_root=str(repo))

    assert recovered["path"] == rec["path"]
    assert recovered["branch"] == branch
    assert Path(recovered["path"]).exists()
    # The SAME branch, not a fresh one: HEAD of the recovered worktree is
    # still that branch's tip.
    assert _git(Path(recovered["path"]), "rev-parse", "HEAD") == \
        _git(repo, "rev-parse", branch)


def test_ac2_recovery_preserves_unique_commits_on_the_branch(
        repo: Path) -> None:
    """AC-2: the branch is not just level with main -- it carries a real
    unpushed commit. Recovery must not discard it."""
    task_id = "t-unique-commit"
    rec = task_workspace.ensure_workspace(task_id, repo_root=str(repo))
    ws = Path(rec["path"])
    (ws / "work.txt").write_text("real work\n", encoding="utf-8")
    _git(ws, "add", "work.txt")
    _git(ws, "commit", "-qm", "feat: unpushed work")
    real_tip = _git(ws, "rev-parse", "HEAD")

    _git(repo, "worktree", "remove", "--force", str(ws))
    assert not ws.exists()

    recovered = task_workspace.ensure_workspace(task_id, repo_root=str(repo))

    assert _git(Path(recovered["path"]), "rev-parse", "HEAD") == real_tip
    assert (Path(recovered["path"]) / "work.txt").exists()


def test_ac3_no_branch_at_all_falls_back_to_a_fresh_one(repo: Path) -> None:
    """AC-3: a task_id that never had a branch (e.g. the index row was
    hand-deleted along with the branch) still gets a normal fresh
    workspace -- the recovery path must not block the ordinary case."""
    task_id = "t-never-existed"
    rec = task_workspace.ensure_workspace(task_id, repo_root=str(repo))
    assert Path(rec["path"]).exists()
    assert rec["branch"] == f"prism/ws/{task_id}"
