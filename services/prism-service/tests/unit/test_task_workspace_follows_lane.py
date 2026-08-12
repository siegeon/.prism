"""A task workspace advances to its own lane's commits automatically.

ROOT ENABLER for task e0149f1f's stop_if #4. The per-task worktree at
data_dir/task_workspaces/<task_id>/ (task_workspace.ensure_workspace) is
created once at a baseline commit and then never re-examined: the
existing-record fast path (task_workspace.py:196-202) and workspace_for
(task_workspace.py:326-330) both skip straight past any git call. Every
receipt minted against that checkout therefore measures whatever tree
happened to be checked out, not the lane's own commits.

That is what produced the 2026-07-21 false green (memory mx-399f3e /
mx-2aff62): task 5a6837a0 closed on a receipt naming tree=c162b66, a
commit belonging to task 89e90d1a, because its worktree sat at baseline
while the lane's commits landed on feat/ui-redesign. e0149f1f fixed the
DECIDING seat so that class now parks instead of closing; this suite pins
the PRODUCING side - sync_workspace(task_id) - so the manual
`git -C <ws> merge --ff-only` a driver used to run by hand stops being
load-bearing.
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


def _git_rc(cwd: Path, *args: str) -> int:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=60).returncode


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git repo with one commit on main, plus an isolated data dir."""
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


def _make(task_id: str, repo: Path) -> dict:
    return task_workspace.ensure_workspace(task_id, repo_root=str(repo))


def test_ac1_advances_to_lane_commit_landed_from_another_checkout(
        repo: Path) -> None:
    """AC-1: HEAD lags at baseline B; commit C lands on the SAME task
    branch from a different checkout; the next ordinary workspace_for
    call advances HEAD to C with no manual merge run by anyone."""
    task_id = "t-follows"
    ws_rec = _make(task_id, repo)
    ws = Path(ws_rec["path"])
    baseline = ws_rec["baseline"]
    branch = ws_rec["branch"]

    # Free the branch from this worktree so "another checkout" can commit
    # on it - the exact mechanism probed and recorded in this task's plan.
    _git(ws, "checkout", "--detach", baseline)

    # "Another checkout": the main checkout of the SAME repo lands a real
    # commit on the task's own branch.
    _git(repo, "checkout", branch)
    (repo / "c.txt").write_text("lane commit\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "lane commit landed elsewhere")
    c_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    # The production entry point - no manual merge anywhere in this test.
    rec = task_workspace.workspace_for(task_id)

    assert rec is not None and rec["path"] == str(ws)
    assert _git(ws, "rev-parse", "HEAD") == c_sha, (
        "workspace must advance to the lane's own commit")
    assert (ws / "c.txt").exists(), "the lane's file must be materialized"


def test_ac2_dirty_workspace_is_left_untouched(repo: Path) -> None:
    """AC-2: uncommitted edits in the workspace block the sync entirely -
    no file touched, HEAD unmoved, state=dirty with a reason."""
    task_id = "t-dirty"
    ws_rec = _make(task_id, repo)
    ws = Path(ws_rec["path"])
    (ws / "scratch.txt").write_text("unsaved work\n", encoding="utf-8")
    before_head = _git(ws, "rev-parse", "HEAD")
    before_bytes = (ws / "scratch.txt").read_bytes()

    res = task_workspace.sync_workspace(task_id)

    assert res.get("state") == "dirty", res
    assert str(res.get("reason", "")).strip(), "dirty must name a reason"
    assert _git(ws, "rev-parse", "HEAD") == before_head, "HEAD must not move"
    assert (ws / "scratch.txt").read_bytes() == before_bytes, (
        "no file in a dirty workspace may be touched")


def test_ac3_never_adopts_a_foreign_branch(repo: Path) -> None:
    """AC-3: a foreign branch (another task's, or main) that the workspace
    could technically fast-forward to is never adopted - the sync follows
    ONLY the task's own recorded branch."""
    task_id = "t-foreign"
    ws_rec = _make(task_id, repo)
    ws = Path(ws_rec["path"])
    head_before = _git(ws, "rev-parse", "HEAD")

    # A DIFFERENT task's branch, carrying a commit descended from the same
    # point - technically fast-forwardable, and forbidden anyway.
    _git(repo, "branch", "prism/ws/other-task", head_before)
    _git(repo, "checkout", "prism/ws/other-task")
    (repo / "foreign.txt").write_text("someone else's work\n",
                                       encoding="utf-8")
    _git(repo, "add", "foreign.txt")
    _git(repo, "commit", "-qm", "a different task's commit")
    _git(repo, "checkout", "main")

    res = task_workspace.sync_workspace(task_id)

    assert _git(ws, "rev-parse", "HEAD") == head_before, (
        "own branch never moved; the sync must not touch another branch")
    assert _git_rc(ws, "cat-file", "-e", "HEAD:foreign.txt") != 0, (
        "the foreign task's file must never be adopted")
    assert res.get("state") != "advanced", res


def test_ac4_diverged_head_is_refused_and_names_both_shas(
        repo: Path) -> None:
    """AC-4: HEAD and the branch tip each carry commits the other lacks -
    refused, never a silent no-op, and the reason names BOTH shas."""
    task_id = "t-diverged"
    ws_rec = _make(task_id, repo)
    ws = Path(ws_rec["path"])
    branch = ws_rec["branch"]
    baseline = ws_rec["baseline"]

    # The worktree commits its OWN change on a detached HEAD ...
    _git(ws, "checkout", "--detach", baseline)
    (ws / "own.txt").write_text("worktree's own work\n", encoding="utf-8")
    _git(ws, "add", "own.txt")
    _git(ws, "commit", "-qm", "worktree diverges")
    own_sha = _git(ws, "rev-parse", "HEAD")

    # ... while the branch ref itself advances elsewhere, from a
    # DIFFERENT starting point (the shared baseline), so neither side is
    # an ancestor of the other.
    _git(repo, "checkout", branch)
    (repo / "elsewhere.txt").write_text("branch tip moved\n",
                                         encoding="utf-8")
    _git(repo, "add", "elsewhere.txt")
    _git(repo, "commit", "-qm", "branch tip advanced without the worktree")
    tip_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    res = task_workspace.sync_workspace(task_id)

    assert res.get("state") == "diverged", res
    reason = str(res.get("reason", ""))
    assert own_sha in reason and tip_sha in reason, (
        f"reason must name both shas: {reason!r}")
    # Refused, not silently no-op'd: nothing moved.
    assert _git(ws, "rev-parse", "HEAD") == own_sha


def test_ac7_sync_failure_still_returns_the_stale_record(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-7: if the sync raises or git is unavailable, workspace_for still
    returns the (stale but usable) record rather than None or a raise."""
    task_id = "t-sync-raises"
    ws_rec = _make(task_id, repo)

    def _boom(_task_id: str) -> dict:
        raise RuntimeError("git is unavailable")

    monkeypatch.setattr(task_workspace, "sync_workspace", _boom)

    rec = task_workspace.workspace_for(task_id)

    assert rec is not None, "a sync failure must not strand the caller"
    assert rec["path"] == ws_rec["path"]
