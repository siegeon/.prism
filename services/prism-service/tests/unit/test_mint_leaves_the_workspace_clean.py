"""A generated law test must never dirty the tree the gate is judging.

Task 84a91b0b, reproduced deterministically 2026-08-29 on task f61617c1:

    clear the worktree      -> git status --porcelain = 0
    POST /api/conductor/gate/mint
    read the worktree again -> git status --porcelain = 1
      M services/prism-service/tests/unit/law/test_promoted_*.py

and readiness then answered "1 uncommitted change(s) remain in the task's
own workspace -- the implementation was never committed, so it cannot be
shipped". Two tasks that were green, cleanly rebased and already merged to
origin/main could not close on it.

CAUSE: task_workspace._prism_repo_root() ascends from ITS OWN MODULE, so an
oracle running inside a task worktree -- importing that worktree's copy of
prism_service -- resolves the root to the WORKTREE and writes there.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import law_promotion as lp  # noqa: E402
from prism_service.services import task_workspace  # noqa: E402


_TTL = "@prefix o: <urn:prism:onto:> .\n"


def test_a_mint_does_not_modify_the_task_worktree(tmp_path, monkeypatch):
    """When the resolved repo root IS a task workspace, write nothing."""
    ws_root = tmp_path / "task_workspaces"
    fake_worktree = ws_root / "72ccaf94-0000-0000-0000-000000000000"
    (fake_worktree / lp._LAW_TESTS_RELDIR).mkdir(parents=True)
    # A linked worktree's .git is a FILE, not a directory.
    (fake_worktree / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")

    monkeypatch.setattr(task_workspace, "_root", lambda: ws_root)
    monkeypatch.setattr(task_workspace, "_prism_repo_root",
                        lambda: fake_worktree)

    ref = lp._write_verification_test("a-rule", _TTL, "v", "c")

    written = list((fake_worktree / lp._LAW_TESTS_RELDIR).glob("*.py"))
    assert written == [], (
        "the generated law test was written INTO the graded worktree -- this "
        f"is what dirtied the tree the gate then judged: {written}")
    assert ref.endswith(
        "_fires_on_violating_and_stays_quiet_on_compliant"), ref


def test_a_normal_checkout_still_gets_the_generated_test(tmp_path, monkeypatch):
    """The guard may only skip a WORKSPACE. A real checkout still writes."""
    ws_root = tmp_path / "task_workspaces"
    ws_root.mkdir(parents=True)
    checkout = tmp_path / "prism"
    (checkout / lp._LAW_TESTS_RELDIR).mkdir(parents=True)
    (checkout / ".git").mkdir()          # a primary checkout: .git is a DIR

    monkeypatch.setattr(task_workspace, "_root", lambda: ws_root)
    monkeypatch.setattr(task_workspace, "_prism_repo_root", lambda: checkout)

    lp._write_verification_test("a-rule", _TTL, "v", "c")
    written = list((checkout / lp._LAW_TESTS_RELDIR).glob("*.py"))
    assert len(written) == 1, f"the real checkout must still get it: {written}"


def test_the_promoted_law_test_is_written_only_when_its_content_changes(
        tmp_path, monkeypatch):
    """An identical rewrite still bumps mtime and re-dirties a tree."""
    ws_root = tmp_path / "task_workspaces"
    ws_root.mkdir(parents=True)
    checkout = tmp_path / "prism"
    (checkout / lp._LAW_TESTS_RELDIR).mkdir(parents=True)
    (checkout / ".git").mkdir()
    monkeypatch.setattr(task_workspace, "_root", lambda: ws_root)
    monkeypatch.setattr(task_workspace, "_prism_repo_root", lambda: checkout)

    import os

    lp._write_verification_test("a-rule", _TTL, "v", "c")
    dest = next((checkout / lp._LAW_TESTS_RELDIR).glob("*.py"))

    # Stamp the file into the past. Comparing raw mtimes across two calls in
    # the same tick is a granularity race (this assertion was flaky 1 run in
    # 3 before); an epoch stamp makes "was it written" unambiguous.
    os.utime(dest, (0, 0))
    assert dest.stat().st_mtime_ns == 0

    lp._write_verification_test("a-rule", _TTL, "v", "c")
    assert dest.stat().st_mtime_ns == 0, (
        "an unchanged law test was rewritten anyway -- generated churn")

    lp._write_verification_test("a-rule", _TTL + "# changed\n", "v", "c")
    assert dest.stat().st_mtime_ns != 0, (
        "a CHANGED law test must still be written")


def test_a_linked_worktree_is_refused_even_outside_the_workspaces_dir(
        tmp_path, monkeypatch):
    """The FIRST version of this guard compared against
    task_workspace._root() and did not fire in production, because
    resolve_data_dir() answers differently inside the oracle's own
    subprocess so the computed workspaces path never matched the real one.
    A linked worktree's .git being a FILE is a filesystem fact that no
    config can disagree with."""
    ws_root = tmp_path / "unrelated_workspaces"
    ws_root.mkdir()
    stray = tmp_path / "somewhere_else"
    (stray / lp._LAW_TESTS_RELDIR).mkdir(parents=True)
    (stray / ".git").write_text("gitdir: /elsewhere/.git/worktrees/y\n")

    monkeypatch.setattr(task_workspace, "_root", lambda: ws_root)
    monkeypatch.setattr(task_workspace, "_prism_repo_root", lambda: stray)

    lp._write_verification_test("a-rule", _TTL, "v", "c")
    assert list((stray / lp._LAW_TESTS_RELDIR).glob("*.py")) == [], (
        "a linked worktree must be refused wherever it lives")
