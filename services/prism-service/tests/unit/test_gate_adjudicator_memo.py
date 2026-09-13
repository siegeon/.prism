"""gate_adjudicator_memo: the workspace-HEAD half of the adjudication key
(task: adjmemo, 2026-09-13). Cached by the workspace's own `.git/HEAD`
mtime so a settled workspace costs one stat(), never a git subprocess,
once its HEAD stops moving.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import gate_adjudicator_memo as gam  # noqa: E402


def setup_function(_):
    gam.reset_for_tests()


def test_no_workspace_returns_empty_sha(monkeypatch):
    monkeypatch.setattr(
        "prism_service.services.task_workspace.workspace_path",
        lambda tid: "")
    assert gam.workspace_head_sha("nope") == ""


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "a@b.c"],
                   check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"],
                   check=True)
    (path / "f.txt").write_text("1")
    subprocess.run(["git", "-C", str(path), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "one"],
                   check=True)


def test_head_sha_matches_git_rev_parse(tmp_path, monkeypatch):
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setattr(
        "prism_service.services.task_workspace.workspace_path",
        lambda tid: str(repo))
    want = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert gam.workspace_head_sha("t1") == want


def test_head_sha_is_cached_until_head_mtime_moves(tmp_path, monkeypatch):
    repo = tmp_path / "ws"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setattr(
        "prism_service.services.task_workspace.workspace_path",
        lambda tid: str(repo))
    first = gam.workspace_head_sha("t1")

    # Poison the cache entry's stored sha directly -- if a second call
    # still shells out (HEAD's mtime unchanged) it must return the CACHED
    # value, not a freshly computed one, proving no subprocess ran.
    cached_mtime, _ = gam._HEAD_CACHE[str(repo)]
    gam._HEAD_CACHE[str(repo)] = (cached_mtime, "stale-cached-value")
    assert gam.workspace_head_sha("t1") == "stale-cached-value"

    # A real second commit moves HEAD's mtime -- the cache must refresh.
    # Force the mtime forward explicitly: some filesystems' mtime
    # resolution is coarser than this test can run in, so two commits a
    # few milliseconds apart can otherwise land on the same mtime.
    (repo / "f.txt").write_text("2")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "two"],
                   check=True)
    head_file = repo / ".git" / "HEAD"
    future = cached_mtime + 5.0
    os.utime(head_file, (future, future))
    second = gam.workspace_head_sha("t1")
    assert second != "stale-cached-value"
    assert second != first
