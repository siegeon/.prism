"""A finished task's worktree is reaped - but ONLY when nothing is lost.

Measured 2026-08-02: data/task_workspaces held 91 directories totalling
2.39 GB. Every one belonged to a FINISHED task (85 done, 5 cancelled, 1
deleted); not one was in progress. ``remove_workspace`` has existed all
along, its own docstring says "used on task teardown", and a grep across
prism_service/ shows it is called from NOWHERE in the product. The reaper
was written and never wired.

Wiring it naively would be far worse than the leak, because
``remove_workspace`` runs ``git branch -D`` (task_workspace.py:258). On the
same measurement, 16 of those 91 worktrees held commits with NO equivalent
on origin/main, and 3 held uncommitted changes. Reaping those on completion
would have silently destroyed real work - the task says done, so nobody
would look again.

Hence ``reap_if_settled``: reap only a worktree that is CLEAN and whose
commits are already on the base. Anything else survives, with a reason.
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


def test_reaps_a_clean_fully_merged_worktree(repo: Path) -> None:
    """The common case: the task shipped, the worktree adds nothing."""
    ws = _make("t-clean", repo)
    path = Path(ws["path"])
    assert path.exists()

    res = task_workspace.reap_if_settled("t-clean")

    assert res.get("reaped") is True, res
    assert not path.exists(), "a settled worktree must be deleted"
    assert task_workspace.workspace_for("t-clean") is None


def test_refuses_when_the_worktree_has_uncommitted_changes(repo: Path) -> None:
    """3 of the 91 measured worktrees were dirty. Deleting those loses work
    that was never even committed, so it is not recoverable from any ref."""
    ws = _make("t-dirty", repo)
    path = Path(ws["path"])
    (path / "scratch.txt").write_text("unsaved\n", encoding="utf-8")

    res = task_workspace.reap_if_settled("t-dirty")

    assert res.get("reaped") is False
    assert "uncommitted" in str(res.get("reason", "")).lower(), res
    assert path.exists(), "a dirty worktree must survive"


def test_refuses_when_commits_are_not_on_the_base(repo: Path) -> None:
    """16 of the 91 held commits absent from origin/main. remove_workspace
    runs `git branch -D`, so reaping these destroys them outright."""
    ws = _make("t-ahead", repo)
    path = Path(ws["path"])
    (path / "b.txt").write_text("work\n", encoding="utf-8")
    _git(path, "add", "b.txt")
    _git(path, "commit", "-qm", "real work that never merged")

    res = task_workspace.reap_if_settled("t-ahead")

    assert res.get("reaped") is False
    assert "not on" in str(res.get("reason", "")).lower(), res
    assert path.exists(), "an unmerged worktree must survive"
    # The commit is still reachable - nothing was destroyed.
    assert _git(path, "log", "--oneline", "-1")


def test_reaps_once_the_work_has_landed_on_the_base(repo: Path) -> None:
    """The refusal is about EQUIVALENCE, not identity: once the same change
    is on the base, the worktree is redundant and may go."""
    ws = _make("t-landed", repo)
    path = Path(ws["path"])
    (path / "b.txt").write_text("work\n", encoding="utf-8")
    _git(path, "add", "b.txt")
    _git(path, "commit", "-qm", "landed")
    # The same commit is merged into the base branch.
    _git(repo, "merge", "--no-edit", "-q", ws["branch"])

    res = task_workspace.reap_if_settled("t-landed")

    assert res.get("reaped") is True, res
    assert not path.exists()


def test_unknown_task_is_a_noop_not_an_error(repo: Path) -> None:
    res = task_workspace.reap_if_settled("t-nope")
    assert res.get("reaped") is False
    assert res.get("reason")


def test_the_conductor_terminal_path_reaps() -> None:
    """The leak existed because nothing CALLED the reaper. Pin the wiring at
    the one place a task becomes terminally done."""
    src = (Path(task_workspace.__file__).parent.parent
           / "mcp" / "tools.py").read_text(encoding="utf-8")
    idx = src.find('task_svc.update(_task_id, status="done")')
    assert idx != -1, "terminal-done path moved; re-anchor this test"
    window = src[idx:idx + 700]
    assert "reap_if_settled" in window, (
        "conductor_work marks a task done but never reaps its worktree - "
        "that is the 2.39 GB leak this slice exists to close"
    )
