"""Immutable source snapshots for externally executed workflow validation."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


_SENSITIVE_NAMES = {".env", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
_RUNTIME_PARTS = {".overstory", ".pytest_cache", "__pycache__", "web_dist_next"}
_RUNTIME_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pyc", ".log"}


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        check=False, env=env,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def _is_sensitive(path: str) -> bool:
    candidate = Path(path)
    return candidate.name.lower() in _SENSITIVE_NAMES or candidate.suffix.lower() in _SENSITIVE_SUFFIXES


def _is_runtime(path: str) -> bool:
    candidate = Path(path)
    return (
        bool(set(candidate.parts) & _RUNTIME_PARTS)
        or candidate.suffix.lower() in _RUNTIME_SUFFIXES
    )


def capture_source_snapshot(repo_root: str | Path) -> dict:
    """Write and pin a commit for the current tracked + nonignored source."""
    root = Path(repo_root).resolve()
    if not (root / ".git").exists():
        raise RuntimeError(f"{root} is not a git checkout")
    base = _git(root, "rev-parse", "HEAD")
    untracked = [item for item in _git(
        root, "ls-files", "--others", "--exclude-standard",
    ).splitlines() if item]
    runtime = sorted(path for path in untracked if _is_runtime(path))
    included = [path for path in untracked if path not in runtime]
    unsafe = sorted(path for path in included if _is_sensitive(path))
    if unsafe:
        raise RuntimeError(
            "refusing to snapshot secret-like untracked files: " + ", ".join(unsafe)
        )

    descriptor, index_path = tempfile.mkstemp(prefix="prism-snapshot-index-")
    os.close(descriptor)
    os.unlink(index_path)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = index_path
    try:
        _git(root, "read-tree", base, env=env)
        _git(root, "add", "-u", "--", ".", env=env)
        for offset in range(0, len(included), 100):
            _git(root, "add", "--", *included[offset:offset + 100], env=env)
        tree = _git(root, "write-tree", env=env)
        commit = _git(
            root, "commit-tree", tree, "-p", base,
            "-m", "PRISM workflow source snapshot", env=env,
        )
        _git(
            root, "update-ref", f"refs/prism/workflow-snapshots/{commit}", commit,
        )
    finally:
        try:
            os.unlink(index_path)
        except FileNotFoundError:
            pass

    base_tree = _git(root, "rev-parse", f"{base}^{{tree}}")
    return {
        "schemaVersion": 1,
        "repositoryRoot": str(root),
        "baseCommit": base,
        "snapshotCommit": commit,
        "tree": tree,
        "dirty": tree != base_tree,
        "includedUntracked": len(included),
        "excludedRuntime": len(runtime),
    }


def validate_source_snapshot(repo_root: str | Path, commit: str, tree: str) -> None:
    """Fail closed unless the persisted commit exists and names this tree."""
    root = Path(repo_root).resolve()
    if not (root / ".git").exists():
        raise RuntimeError(f"{root} is not a git checkout")
    _git(root, "cat-file", "-e", f"{commit}^{{commit}}")
    actual_tree = _git(root, "rev-parse", f"{commit}^{{tree}}")
    if actual_tree != tree:
        raise RuntimeError("snapshot tree does not match snapshot commit")
