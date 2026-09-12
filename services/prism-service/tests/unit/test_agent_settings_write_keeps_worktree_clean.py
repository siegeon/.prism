"""A task worktree stays clean after its agent settings write (task 7b897fcd).

Commit 37fa182c (2026-09-08) added `_write_agent_settings` in
task_workspace.py, which writes `.claude/settings.local.json` into every
task worktree and trusted the REPO's own `.gitignore` to keep that write
from dirtying `git status`. The test fixtures build scratch repos with no
such line, so on the GitHub runner every scratch worktree showed
'?? .claude/' and the dirty check refused it -- nine tests failed on every
PR since 2026-09-08. Locally they passed only because
~/.config/git/ignore happened to hold `**/.claude/settings.local.json`, a
GLOBAL git config this suite must not rely on either.

This test neutralises both possible masks itself (GIT_CONFIG_GLOBAL and
XDG_CONFIG_HOME), builds a scratch repo with NO .gitignore at all, and
asserts the worktree `ensure_workspace` produces is clean -- so a global
ignore file on the machine that happens to run this suite can never hide
a regression.
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


@pytest.fixture(autouse=True)
def _no_global_git_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Neutralise any global git ignore/config on the HOST running this
    suite, so the defect this test guards against cannot be masked the
    same way it was masked locally before task 7b897fcd was filed."""
    empty_xdg = tmp_path / "xdg_empty"
    empty_xdg.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_xdg))


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git repo with one commit on main and NO .gitignore at all --
    the exact shape of the CI fixture repos that caught this defect."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "base")
    assert not (root / ".gitignore").exists()
    monkeypatch.setattr(task_workspace, "resolve_data_dir",
                        lambda: tmp_path / "data")
    return root


def test_ensure_workspace_leaves_the_worktree_clean(repo: Path) -> None:
    """The oracle: a freshly created worktree, with the agent settings
    write applied, reports no untracked/dirty paths at all."""
    rec = task_workspace.ensure_workspace("t-clean", repo_root=str(repo))
    ws = Path(rec["path"])

    assert (ws / ".claude" / "settings.local.json").exists(), (
        "the settings write itself must still happen")
    status = _git(ws, "status", "--porcelain")
    assert status == "", f"worktree is not clean: {status!r}"


def test_second_call_adds_no_duplicate_exclude_lines(repo: Path) -> None:
    """Idempotent: calling ensure_workspace (which rewrites the settings
    file on every call, by design) twice must not grow the exclude file
    with repeated lines, and the worktree must still be clean."""
    rec = task_workspace.ensure_workspace("t-idempotent", repo_root=str(repo))
    ws = Path(rec["path"])

    exclude = Path(_git(ws, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = ws / exclude
    first_lines = exclude.read_text(encoding="utf-8").splitlines()

    # Call the self-heal path a second time directly (ensure_workspace's
    # fast path for an existing record already does this on every call).
    task_workspace._write_agent_settings(ws)

    second_lines = exclude.read_text(encoding="utf-8").splitlines()
    assert second_lines == first_lines, (
        "a repeat write must not duplicate exclude entries")
    assert len(second_lines) == len(set(second_lines)), (
        "exclude file must have no duplicate lines")
    assert _git(ws, "status", "--porcelain") == ""
