import subprocess
from pathlib import Path

import pytest

from prism_service.services.source_snapshot import capture_source_snapshot


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tests@prism.local")
    _git(root, "config", "user.name", "PRISM Tests")
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (root / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    return root


def test_snapshot_captures_dirty_and_untracked_but_not_ignored(tmp_path):
    root = _repo(tmp_path)
    (root / "tracked.txt").write_text("after\n", encoding="utf-8")
    (root / "new.py").write_text("value = 1\n", encoding="utf-8")
    (root / "ignored.txt").write_text("private\n", encoding="utf-8")
    (root / "tasks.db").write_bytes(b"runtime")

    snapshot = capture_source_snapshot(root)

    assert snapshot["dirty"] is True
    assert snapshot["includedUntracked"] == 1
    assert snapshot["excludedRuntime"] == 1
    assert _git(root, "show", f'{snapshot["snapshotCommit"]}:tracked.txt') == "after"
    assert _git(root, "show", f'{snapshot["snapshotCommit"]}:new.py') == "value = 1"
    missing = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e",
         f'{snapshot["snapshotCommit"]}:ignored.txt'], capture_output=True,
    )
    assert missing.returncode != 0
    assert subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e",
         f'{snapshot["snapshotCommit"]}:tasks.db'], capture_output=True,
    ).returncode != 0


def test_snapshot_rejects_secret_like_untracked_files(tmp_path):
    root = _repo(tmp_path)
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="secret-like"):
        capture_source_snapshot(root)
