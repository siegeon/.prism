"""RED contract — worktree-aware location self-check (task 7faed505).

The test_implement_* suites carry a location guard
(`test_only_canonical_workflow_targeted`) that asserted a blanket
`"worktrees" not in str(_WORKFLOWS)`. That reds EVERY run from a legitimate
agent worktree (.claude/worktrees/<name>) of the canonical repo, so every
lane's full-suite receipt carried a "pre-existing failures" caveat.

New contract, pinned here: the guard delegates to ONE shared helper,
`tests.worktree_guard.worktree_of_canonical(workflows_dir)`, which decides
by GIT IDENTITY — never by path whitelisting:

  * ACCEPT — a linked git worktree of the canonical repo: `git rev-parse`
    reports --git-dir != --git-common-dir (linked, not main checkout) AND
    the common repo's root hosts .claude/workflows/implement.js.
  * REJECT — a bare directory copy under a worktrees-like path (no git
    metadata at all): rev-parse fails or resolves elsewhere.
  * REJECT — a genuinely foreign clone (its own .git: git-dir ==
    git-common-dir), even when parked under a path containing "worktrees".

The original protection (the suite must assert against the canonical
.claude/workflows/implement.js, never a stray copy) is therefore KEPT —
only the false positive on legitimate worktrees is removed.

ALL RED today: tests/worktree_guard.py does not exist and both guard suites
still hard-assert the blanket path check.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _helper():
    """Lazy import so the RED state fails each test (not collection)."""
    from tests.worktree_guard import worktree_of_canonical

    return worktree_of_canonical


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=guard@test",
         "-c", "user.name=guard-test", *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def canonical_repo(tmp_path: Path) -> Path:
    """A throwaway 'canonical' repo hosting .claude/workflows/implement.js."""
    repo = tmp_path / "canonical"
    wf = repo / ".claude" / "workflows"
    wf.mkdir(parents=True)
    (wf / "implement.js").write_text("// canonical implement.js\n",
                                     encoding="utf-8")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed canonical workflow")
    return repo


# -- ACCEPT: a linked git worktree of the canonical repo ---------------------

def test_linked_worktree_of_canonical_is_accepted(canonical_repo: Path):
    """The real agent layout: `git worktree add` under
    <canonical>/.claude/worktrees/<name>. Its .claude/workflows must be a
    legitimate home for the guard suites."""
    wt = canonical_repo / ".claude" / "worktrees" / "agent-guardtest"
    _git(canonical_repo, "worktree", "add", "-q", str(wt))
    workflows = wt / ".claude" / "workflows"
    assert workflows.is_dir(), "worktree checkout must carry the workflows dir"
    assert "worktrees" in str(workflows)  # the exact string the old guard red on
    assert _helper()(workflows) is True, (
        "a linked git worktree of the canonical repo must be ACCEPTED as a "
        "legitimate home (git-common-dir points back at the canonical .git)"
    )


# -- REJECT: a bare copy parked under a worktrees-like path ------------------

def test_bare_copy_under_worktrees_path_is_rejected(canonical_repo: Path,
                                                    tmp_path: Path):
    """A plain file copy (no git metadata) placed under .claude/worktrees/*
    is exactly the stray the original guard existed to catch — still red."""
    copy = tmp_path / "elsewhere" / ".claude" / "worktrees" / "agent-copy"
    shutil.copytree(canonical_repo, copy,
                    ignore=shutil.ignore_patterns(".git"))
    workflows = copy / ".claude" / "workflows"
    assert (workflows / "implement.js").exists()
    assert _helper()(workflows) is False, (
        "a bare directory copy (no git identity) must still be REJECTED"
    )


# -- REJECT: a genuinely foreign clone ----------------------------------------

def test_foreign_clone_is_rejected(canonical_repo: Path, tmp_path: Path):
    """An independent clone owns its own .git (git-dir == git-common-dir):
    it is NOT a linked worktree of the canonical repo and stays rejected,
    even when parked under a path containing 'worktrees'."""
    clone = tmp_path / "worktrees" / "foreign-clone"
    _git(tmp_path, "clone", "-q", str(canonical_repo), str(clone))
    workflows = clone / ".claude" / "workflows"
    assert (workflows / "implement.js").exists()
    assert _helper()(workflows) is False, (
        "a foreign clone (its own .git) must still be REJECTED — worktree "
        "awareness must not blanket-whitelist every git checkout"
    )


# -- The two guard suites must share THIS helper (no drift) -------------------

def test_guard_suites_delegate_to_shared_helper():
    """Both test_implement_* location guards must consume the ONE shared
    helper, so the accept/reject contract cannot drift per-file."""
    from tests import worktree_guard
    from tests.integration import (
        test_implement_dependency_aware_branch_base as branch_base,
        test_implement_source_of_truth_writeback as writeback,
    )

    assert getattr(branch_base, "worktree_of_canonical", None) \
        is worktree_guard.worktree_of_canonical, (
        "dependency_aware_branch_base guard must import the shared helper"
    )
    assert getattr(writeback, "worktree_of_canonical", None) \
        is worktree_guard.worktree_of_canonical, (
        "source_of_truth_writeback guard must import the shared helper"
    )
