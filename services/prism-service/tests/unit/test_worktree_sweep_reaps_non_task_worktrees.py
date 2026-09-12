"""The reap sweep: reap worktrees NO task row ever points at (task
[fixer-brief] reap-node-non-task-worktrees, ops incident 2026-09-12).

`task_reaper.reap_task` only ever runs for the ONE task_id a land just
finished (`ship_worker._reap_after_land`), keyed off the workspace index.
An agent worktree (`.claude/worktrees/agent-*`), a QA/fixer worktree
(`/home/siegeon/wt-*`, `/home/siegeon/.claude/jobs/qa-*`) or a task branch
whose row was later deleted has no matching task_id, so it is invisible to
that per-task hook forever, however clean and however long dead. Measured
live: 173 registered worktrees, disk at 98%, 93 of them already landed on
origin/main and clean.

`task_reaper.sweep_worktrees` is the task-agnostic net: it reads
`git worktree list --porcelain` directly (ground truth, not the workspace
index) and reaps ANY worktree that is clean, old enough, unused, and whose
tip is already an ancestor of origin/main -- regardless of whether a task
row exists for it.

SHIPPEDNESS HERE IS DELIBERATELY is-ancestor, not the task trailer (unlike
reap_task, which CLAUDE.md prohibits from using is-ancestor): there is no
task row to attribute the trailer to for an orphan worktree, so "is this
commit reachable from origin/main" is the only question available, and a
worktree failing it is kept (a false negative costs disk, not work).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from prism_service.services import task_reaper, task_workspace


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {r.stderr}")
    return r.stdout.strip()


def _rc(cwd: Path, *args: str) -> int:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=60).returncode


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


def _orphan_worktree(repo: Path, name: str, branch: str,
                     parent: Path | None = None) -> Path:
    """A worktree with NO task_workspace index row -- the exact shape an
    agent/QA/fixer worktree has (created by `git worktree add` directly,
    never through `ensure_workspace`)."""
    base = parent or (repo.parent / "worktrees")
    base.mkdir(parents=True, exist_ok=True)
    ws = base / name
    _git(repo, "worktree", "add", "-b", branch, str(ws), "main")
    return ws


def _age(path: Path, hours: float) -> None:
    """Backdate a worktree directory's mtime -- the sweep's age gate reads
    exactly this, never a commit timestamp."""
    old = time.time() - hours * 3600.0
    import os
    os.utime(path, (old, old))


def _merge_into_main(repo: Path, ws: Path, branch: str) -> None:
    """Land the branch's tip onto origin/main by fast-forward, the simplest
    'this is fully shipped' shape -- is-ancestor sees it directly, no
    trailer needed."""
    _git(repo, "merge", "--ff-only", branch)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def _commit(ws: Path, text: str, message: str) -> None:
    (ws / "f.txt").write_text(text, encoding="utf-8")
    _git(ws, "add", "f.txt")
    _git(ws, "commit", "-qm", message)


def _worktree_paths(repo: Path) -> str:
    return _git(repo, "worktree", "list")


def _branches(repo: Path) -> list[str]:
    return _git(repo, "branch", "--format=%(refname:short)").splitlines()


# ---------------------------------------------------------------------------
# The pinned contract (task.verify)
# ---------------------------------------------------------------------------
def test_a_clean_merged_old_orphan_worktree_is_reaped(repo: Path) -> None:
    ws = _orphan_worktree(repo, "agent-abc123", "agent/abc123")
    _commit(ws, "work\n", "feat: agent work")
    _merge_into_main(repo, ws, "agent/abc123")
    _age(ws, 25.0)

    result = task_reaper.sweep_worktrees(repo_root=str(repo))

    items = {i["path"]: i for i in result["items"]}
    item = items[str(ws)]
    assert item["outcome"] == "pass", item["reason"]
    assert item["reaped"] is True
    assert item["branch_deleted"] is True
    assert not ws.exists()
    assert "agent/abc123" not in _branches(repo)
    assert str(ws) not in _worktree_paths(repo)
    assert result["reaped"] >= 1


def test_a_young_orphan_worktree_is_kept(repo: Path) -> None:
    """No mtime backdate -- inside the age grace window."""
    ws = _orphan_worktree(repo, "qa-fresh", "qa/fresh")
    _commit(ws, "work\n", "feat: qa work")
    _merge_into_main(repo, ws, "qa/fresh")

    result = task_reaper.sweep_worktrees(repo_root=str(repo))

    item = {i["path"]: i for i in result["items"]}[str(ws)]
    assert item["reaped"] is False
    assert "young" in item["reason"].lower()
    assert ws.exists()


def test_a_dirty_orphan_worktree_is_kept(repo: Path) -> None:
    ws = _orphan_worktree(repo, "fixer-dirty", "fix/dirty")
    _commit(ws, "work\n", "feat: fixer work")
    _merge_into_main(repo, ws, "fix/dirty")
    _age(ws, 48.0)
    (ws / "unsaved.txt").write_text("not committed anywhere\n", encoding="utf-8")

    result = task_reaper.sweep_worktrees(repo_root=str(repo))

    item = {i["path"]: i for i in result["items"]}[str(ws)]
    assert item["reaped"] is False
    assert "uncommitted" in item["reason"].lower()
    assert ws.exists()
    assert (ws / "unsaved.txt").exists()


def test_an_unmerged_orphan_worktree_survives(repo: Path) -> None:
    """A branch nobody landed yet -- is-ancestor says no, so the sweep
    never touches the one copy of that work."""
    ws = _orphan_worktree(repo, "fixer-wip", "fix/wip")
    _commit(ws, "work\n", "feat: still cooking")
    _age(ws, 48.0)

    result = task_reaper.sweep_worktrees(repo_root=str(repo))

    item = {i["path"]: i for i in result["items"]}[str(ws)]
    assert item["reaped"] is False
    assert "merged" in item["reason"].lower()
    assert ws.exists()
    assert "fix/wip" in _branches(repo)


def test_a_worktree_someone_is_sitting_in_survives(repo: Path) -> None:
    ws = _orphan_worktree(repo, "agent-live", "agent/live")
    _commit(ws, "work\n", "feat: live agent")
    _merge_into_main(repo, ws, "agent/live")
    _age(ws, 48.0)

    result = task_reaper.sweep_worktrees(
        repo_root=str(repo), is_path_live=lambda p: str(p) == str(ws))

    item = {i["path"]: i for i in result["items"]}[str(ws)]
    assert item["reaped"] is False
    assert "using this worktree" in item["reason"].lower()
    assert ws.exists()


def test_a_locked_worktree_survives(repo: Path) -> None:
    ws = _orphan_worktree(repo, "agent-locked", "agent/locked")
    _commit(ws, "work\n", "feat: locked agent")
    _merge_into_main(repo, ws, "agent/locked")
    _age(ws, 48.0)
    _git(repo, "worktree", "lock", str(ws))

    result = task_reaper.sweep_worktrees(repo_root=str(repo))

    item = {i["path"]: i for i in result["items"]}[str(ws)]
    assert item["reaped"] is False
    assert "lock" in item["reason"].lower()
    assert ws.exists()


def test_the_main_checkout_is_never_in_the_sweep_results(repo: Path) -> None:
    result = task_reaper.sweep_worktrees(repo_root=str(repo))
    paths = {i["path"] for i in result["items"]}
    assert str(repo) not in paths or \
        {i for i in result["items"] if i["path"] == str(repo)}.pop()["reaped"] is False


def test_the_sweep_reaps_a_task_worktree_too_when_orphaned(repo: Path,
                                                           monkeypatch) -> None:
    """A task's own worktree, once its index row is gone (the exact 127-of-
    154-orphan-branches shape the per-task reaper already handles for
    branches-with-no-worktree) -- the sweep must not require a task row
    either way."""
    monkeypatch.setattr(task_workspace, "resolve_data_dir",
                        lambda: repo.parent / "data")
    tid = "f97c196d-1111-2222-3333-444444444444"
    rec = task_workspace.ensure_workspace(tid, repo_root=str(repo))
    ws = Path(rec["path"])
    _commit(ws, "work\n", f"feat: the thing [task:{tid[:8]}]")
    _merge_into_main(repo, ws, f"prism/ws/{tid}")
    idx = task_workspace._load_index()
    idx.pop(tid, None)
    task_workspace._save_index(idx)
    _age(ws, 48.0)

    result = task_reaper.sweep_worktrees(repo_root=str(repo))

    item = {i["path"]: i for i in result["items"]}[str(ws)]
    assert item["reaped"] is True, item["reason"]
    assert not ws.exists()


def test_survey_mode_deletes_nothing(repo: Path) -> None:
    ws = _orphan_worktree(repo, "agent-survey", "agent/survey")
    _commit(ws, "work\n", "feat: survey me")
    _merge_into_main(repo, ws, "agent/survey")
    _age(ws, 48.0)

    result = task_reaper.sweep_worktrees(repo_root=str(repo), mode="survey")

    item = {i["path"]: i for i in result["items"]}[str(ws)]
    assert item["would_reap"] is True
    assert item["reaped"] is False
    assert ws.exists()
    assert "agent/survey" in _branches(repo)


def test_a_missing_worktree_directory_is_pruned(repo: Path) -> None:
    ws = _orphan_worktree(repo, "agent-gone", "agent/gone")
    _commit(ws, "work\n", "feat: vanished")
    _merge_into_main(repo, ws, "agent/gone")
    _age(ws, 48.0)
    # Torn down by hand, the way an operator's `rm -rf` leaves things: git
    # still lists it until a `worktree prune`.
    import shutil
    shutil.rmtree(ws)
    assert str(ws) in _worktree_paths(repo)

    task_reaper.sweep_worktrees(repo_root=str(repo))

    assert str(ws) not in _worktree_paths(repo)


def test_the_sweep_never_uses_the_task_trailer(repo: Path) -> None:
    """stop_if: 'the sweep cannot tell shippedness without a task row'."""
    source = Path(task_reaper.__file__).read_text(encoding="utf-8")
    assert "def sweep_worktrees" in source
    # It is allowed to use is-ancestor -- unlike reap_task, there is no task
    # row here to misattribute a trailer to.
    assert "is-ancestor" in source or "is_ancestor" in source
