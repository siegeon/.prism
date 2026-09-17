"""The worker identity belongs to the task worktree, never to the checkout
that hosts it.

`ensure_workspace` sets `user.email`/`user.name` inside the new task
worktree. A LINKED worktree shares `.git/config` with its parent checkout,
so a plain `git config` call there writes the identity for the WHOLE
repository. Every later commit in the parent checkout then carries
`prism-worker <worker@prism>`, and the developer sees no warning.

Found live, 2026-09-16. A PRODUCT repository, and not a PRISM checkout,
collected 15 commits with the author `prism-worker <worker@prism>`. All 15
reached the remote inside an open pull request. `worker@prism` reaches no
mailbox and matches no GitHub account, so GitHub shows such a commit as
unattributed. The developer had no global `user.email`, so git read the
leaked value as the only identity.

`ensure_workspace(task_id, repo_root=...)` accepts ANY checkout as its
root. So the fault reaches more than PRISM's own tree. The default path
leaks too: there the root is the PRISM checkout.

These tests pin the two halves that must hold together. The parent keeps
its own identity, and the worktree still commits as the worker.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from prism_service.services import task_workspace

_REAL_EMAIL = "real@corp.example"
_REAL_NAME = "Real Developer"


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {r.stderr}")
    return r.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A parent checkout that already holds the developer's own identity."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", _REAL_EMAIL)
    _git(root, "config", "user.name", _REAL_NAME)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "base")
    monkeypatch.setattr(task_workspace, "resolve_data_dir",
                        lambda: tmp_path / "data")
    return root


def test_ac1_the_parent_checkout_keeps_its_own_identity(repo: Path) -> None:
    """AC-1: the live fault. After ensure_workspace, the parent checkout
    still commits as the developer, and its shared config file holds no
    worker address at all."""
    task_workspace.ensure_workspace("t-identity", repo_root=str(repo))

    assert _git(repo, "config", "--get", "user.email") == _REAL_EMAIL
    assert _git(repo, "config", "--get", "user.name") == _REAL_NAME

    shared = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert "worker@prism" not in shared
    assert "prism-worker" not in shared


def test_ac2_the_worktree_still_commits_as_the_worker(repo: Path) -> None:
    """AC-2: the isolation must not remove the feature. The worker identity
    is still the one git uses inside the task worktree."""
    rec = task_workspace.ensure_workspace("t-worker-id", repo_root=str(repo))
    ws = Path(rec["path"])

    assert _git(ws, "config", "--get", "user.email") == "worker@prism"
    assert _git(ws, "config", "--get", "user.name") == "prism-worker"


def test_ac3_a_real_commit_in_each_checkout_carries_the_right_author(
        repo: Path) -> None:
    """AC-3: the assertion a person can see. Commit in both checkouts and
    read the author of each. Config reads alone proved too little: the leak
    showed itself in `git log`, not in a config file the developer opened."""
    rec = task_workspace.ensure_workspace("t-two-authors", repo_root=str(repo))
    ws = Path(rec["path"])

    (ws / "work.txt").write_text("task work\n", encoding="utf-8")
    _git(ws, "add", "work.txt")
    _git(ws, "commit", "-qm", "feat: task work")

    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "chore: developer work")

    assert _git(ws, "log", "-1", "--format=%an <%ae>") == \
        "prism-worker <worker@prism>"
    assert _git(repo, "log", "-1", "--format=%an <%ae>") == \
        f"{_REAL_NAME} <{_REAL_EMAIL}>"
