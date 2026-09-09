"""The conductor pipeline INVOKES the reap on a successful land (task f97c196d).

This suite pins that ship_task's completed path actually calls _reap_after_land,
which removes a shipped task's git worktree and its `prism/ws/<task_id>` branch.

The unit-level test_pipeline_reaps_a_finished_task.py verifies that the reap
function itself works correctly (safety rules, refusals, edge cases). This suite
verifies that the pipeline actually INVOKES it on the shipped path, so the
artifacts of a completed drive do not accumulate on disk.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

TASK_ID = "f97c196d-0000-0000-0000-000000000000"


def _git(cwd: Path, *args: str) -> str:
    """Run git in the repo, raise on error."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=60, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> rc={r.returncode} "
                          f"stderr={r.stderr}")
    return r.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    """Write a file to the repo."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _branches(repo: Path) -> list[str]:
    """List all branches in the repo."""
    try:
        return _git(repo, "branch", "--format=%(refname:short)").splitlines()
    except RuntimeError:
        return []


def _worktree_list(repo: Path) -> list[str]:
    """List all worktrees in the repo."""
    try:
        lines = _git(repo, "worktree", "list", "--porcelain").splitlines()
        out = []
        for line in lines:
            if line.startswith("worktree "):
                out.append(line[len("worktree "):].strip())
        return out
    except RuntimeError:
        return []


def _shipped_workspace(tmp_path: Path, task_id: str = TASK_ID) -> tuple[Path, Path, str, Path]:
    """A repo with a task worktree that has been shipped (merged to main).

    Returns (origin, main_checkout, branch, worktree_path).
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", str(origin))

    main_checkout = tmp_path / "main"
    _git(tmp_path, "clone", "-q", str(origin), str(main_checkout))
    _write(main_checkout, "README.md", "# baseline\n")
    _git(main_checkout, "add", "-A")
    _git(main_checkout, "commit", "-qm", "baseline")
    _git(main_checkout, "branch", "-M", "main")
    _git(main_checkout, "push", "-q", "origin", "main")

    # Create a task branch and commit work.
    branch = f"prism/ws/{task_id}"
    _git(main_checkout, "checkout", "-q", "-b", branch)
    _write(main_checkout, "work.txt", "work\n")
    _git(main_checkout, "add", "-A")
    _git(main_checkout, "commit", "-qm", f"feat: the work [task:{task_id[:8]}]")
    _git(main_checkout, "push", "-q", "origin", branch)

    # Create a worktree for the task branch (this is what happens in production).
    # First, checkout main so the branch is not currently checked out.
    _git(main_checkout, "checkout", "-q", "main")
    worktree_path = tmp_path / "worktrees" / task_id
    _git(main_checkout, "worktree", "add", str(worktree_path), branch)

    # Simulate a squash-merge: create a sibling commit on main with the trailer.
    _write(main_checkout, "shipped.txt", "shipped\n")
    _git(main_checkout, "add", "-A")
    _git(main_checkout, "commit", "-qm", f"feat: squash-merged [task:{task_id[:8]}]")
    _git(main_checkout, "push", "-q", "origin", "main")

    return origin, main_checkout, branch, worktree_path


def test_reap_removes_shipped_task_worktree_and_branch(tmp_path: Path) -> None:
    """AC: after ship_task completes for a shipped task, the worktree is gone
    and the prism/ws/<id> branch is deleted from the repo."""
    from prism_service.services import ship_worker, task_workspace

    origin, main_checkout, branch, worktree_path = _shipped_workspace(tmp_path)
    assert branch in _branches(main_checkout), "fixture must include the task branch"
    assert worktree_path.exists(), "fixture must create the worktree"

    # Mock both workspace_for (used by ship_task) and workspace_record (used by reap_task).
    ws_rec = {"task_id": TASK_ID, "path": str(worktree_path), "branch": branch, "repo_root": str(main_checkout)}

    with mock.patch.object(task_workspace, "workspace_for", return_value=ws_rec):
        with mock.patch.object(task_workspace, "workspace_record", return_value=ws_rec):
            # Mock get_project to support _reap_after_land.
            mock_project = mock.MagicMock()
            mock_project._data_dir = tmp_path / "project_data"
            mock_project._data_dir.mkdir(parents=True, exist_ok=True)
            (mock_project._data_dir / "scores.db").touch()

            def fake_runner(argv, cwd=None):
                if argv[0] != "gh":
                    r = subprocess.run(
                        argv, cwd=str(cwd) if cwd else None,
                        capture_output=True, text=True,
                        env={**os.environ, "GIT_AUTHOR_NAME": "t",
                             "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                             "GIT_COMMITTER_EMAIL": "t@t"})
                    return r.returncode, r.stdout or "", r.stderr or ""

                head = " ".join(str(a) for a in argv[:3])
                if "pr create" in head:
                    return 0, "https://github.com/x/y/pull/1\n", ""
                if "pr checks" in head:
                    return 0, "all checks passed\n", ""
                if "pr merge" in head:
                    # Already merged in the fixture.
                    return 0, "merged\n", ""
                if "auth status" in head:
                    return 0, "Logged in\n", ""
                return 0, "", ""

            # Call ship_task with mocked project context.
            with mock.patch("prism_service.project_context.get_project",
                           return_value=mock_project):
                res = ship_worker.ship_task(TASK_ID, runner=fake_runner, poll_interval_s=0)
                assert res.get("ok") is True, f"ship_task failed: {res}"

    # After shipping, the worktree and branch must be gone.
    _git(main_checkout, "fetch", "-q", "origin")
    assert branch not in _branches(main_checkout), \
        f"branch {branch} should be deleted after reap, but found: {_branches(main_checkout)}"
    assert str(worktree_path) not in _worktree_list(main_checkout), \
        f"worktree {worktree_path} should be removed after reap, but found: {_worktree_list(main_checkout)}"
    assert not worktree_path.exists(), \
        f"worktree path {worktree_path} should be deleted after reap"


def test_reap_respects_the_no_dirty_worktree_rule(tmp_path: Path) -> None:
    """AC: if the shipped worktree has uncommitted changes, reap refuses
    (never deletes) the worktree and branch."""
    from prism_service.services import ship_worker, task_workspace

    origin, main_checkout, branch, worktree_path = _shipped_workspace(tmp_path)

    # Leave uncommitted work in the worktree.
    _write(worktree_path, "unsaved.txt", "uncommitted\n")

    ws_rec = {"task_id": TASK_ID, "path": str(worktree_path), "branch": branch, "repo_root": str(main_checkout)}

    with mock.patch.object(task_workspace, "workspace_for", return_value=ws_rec):
        with mock.patch.object(task_workspace, "workspace_record", return_value=ws_rec):
            # Mock get_project to support _reap_after_land.
            mock_project = mock.MagicMock()
            mock_project._data_dir = tmp_path / "project_data"
            mock_project._data_dir.mkdir(parents=True, exist_ok=True)
            (mock_project._data_dir / "scores.db").touch()

            def fake_runner(argv, cwd=None):
                if argv[0] != "gh":
                    r = subprocess.run(
                        argv, cwd=str(cwd) if cwd else None,
                        capture_output=True, text=True,
                        env={**os.environ, "GIT_AUTHOR_NAME": "t",
                             "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                             "GIT_COMMITTER_EMAIL": "t@t"})
                    return r.returncode, r.stdout or "", r.stderr or ""

                head = " ".join(str(a) for a in argv[:3])
                if "pr create" in head:
                    return 0, "https://github.com/x/y/pull/1\n", ""
                if "pr checks" in head:
                    return 0, "all checks passed\n", ""
                if "pr merge" in head:
                    return 0, "merged\n", ""
                if "auth status" in head:
                    return 0, "Logged in\n", ""
                return 0, "", ""

            # Call ship_task with mocked project context.
            with mock.patch("prism_service.project_context.get_project",
                           return_value=mock_project):
                res = ship_worker.ship_task(TASK_ID, runner=fake_runner, poll_interval_s=0)
                assert res.get("ok") is True, f"ship_task failed: {res}"

    # The reap must have refused the worktree because of uncommitted changes.
    _git(main_checkout, "fetch", "-q", "origin")
    assert branch in _branches(main_checkout), \
        f"branch {branch} should survive (reap refused), but it was deleted"
    assert str(worktree_path) in _worktree_list(main_checkout), \
        f"worktree {worktree_path} should survive (reap refused), but it was removed"
    assert (worktree_path / "unsaved.txt").exists(), \
        "uncommitted file should still be there"
