"""Regression test for task f5dd6dc5: root pyproject testpaths must not point at ghosts.

The repo-root pyproject.toml [tool.pytest.ini_options] testpaths once referenced
plugins/prism-devtools/tools/prism-cli/tests, a directory that no longer exists.
pytest 9 then silently falls back to FULL recursive rootdir collection (the
Tier0 collection-explosion enabler, 271dacc9). Pin the invariant: every entry
in root testpaths exists on disk and contains at least one test file.
"""
from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def _repo_root() -> Path:
    """Walk up from this file to the repo root (dir holding root pyproject.toml)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "plugins").is_dir():
            return parent
    raise AssertionError("repo root with pyproject.toml + plugins/ not found")


def test_root_testpaths_entries_exist_and_hold_tests():
    root = _repo_root()
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    testpaths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    assert testpaths, "root testpaths must not be empty"
    for entry in testpaths:
        target = root / entry
        assert target.is_dir(), (
            f"root pyproject testpaths entry '{entry}' does not exist - "
            "pytest will warn and fall back to full rootdir collection"
        )
        assert any(target.rglob("test_*.py")), (
            f"root pyproject testpaths entry '{entry}' contains no test files"
        )
