"""commit-tests-only must recognise a test file inside a brand-new,
untracked directory (e.g. tests/unit/test_alpha.py), not just a bare
test_*.py sitting at the worktree root.

THE DEFECT (measured live: commit-tests-only ok=0 x 25, ok=1 x 0 -- it has
NEVER succeeded). `git status --porcelain` collapses a wholly-untracked
directory into a single entry with a trailing slash (`?? tests/`) instead
of listing the files inside it. workflow_step_commit_tests_only then does
`f.rsplit("/", 1)[-1]` on "tests/", which is the empty string -- it does
not start with "test_", so the directory is classified as a stray
non-test path and the whole commit is refused. A brand-new test file
almost always lands in a directory git has not seen before, so this
misfires every time (api/workflows.py ~4217-4232).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from prism_service.api import workflows as wf


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.example"],
                   cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def test_a_test_file_in_a_brand_new_untracked_directory_is_committed(
        monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    _init_repo(root)
    # tests/unit/ does not exist yet as far as git is concerned -- this is
    # the shape every real red-step commit actually has.
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "test_alpha.py").write_text(
        "def test_alpha():\n    assert True\n")
    monkeypatch.setattr(wf, "_task_worktree", lambda task_id: (root, ""))
    monkeypatch.setattr(wf, "_record_node_run", lambda *a, **k: None)

    body = wf.CommitTestsOnlyRequest(task_id="t-1")
    out = wf.workflow_step_commit_tests_only(body, project="p")

    assert out["outcome"] == "ok", out
    assert out["committed"] is True, out
    assert out["files"] == ["tests/unit/test_alpha.py"], out

    log = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=True)
    assert "tests/unit/test_alpha.py" in log.stdout, log.stdout


def test_a_dirty_non_test_file_alongside_a_test_file_still_refuses(
        monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    _init_repo(root)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "test_alpha.py").write_text(
        "def test_alpha():\n    assert True\n")
    (root / "prism_service").mkdir()
    (root / "prism_service" / "services").mkdir()
    (root / "prism_service" / "services" / "foo.py").write_text("x = 1\n")
    monkeypatch.setattr(wf, "_task_worktree", lambda task_id: (root, ""))
    monkeypatch.setattr(wf, "_record_node_run", lambda *a, **k: None)

    body = wf.CommitTestsOnlyRequest(task_id="t-1")
    out = wf.workflow_step_commit_tests_only(body, project="p")

    assert out["outcome"] == "refused", out
    assert out["committed"] is False, out
    assert "prism_service/services/foo.py" in out["reason"], out

    log = subprocess.run(["git", "log", "--oneline"], cwd=root,
                         capture_output=True, text=True)
    assert log.returncode != 0 or log.stdout.strip() == "", (
        "a stray non-test file must block the commit entirely: " + log.stdout)
