"""Regression test for task c944660b: PRISM data dirs must be outside pytest reach.

The data dir (e.g. data-selfimprove/) lives untracked inside the repo tree and
holds a full stale repo mirror at projects/prism/graphify-src/** (staged by
graph_service.py: self._project_dir / "graphify-src"). Recursive collection
descending into it shadowed test modules (Tier0 collection explosion).
Pin the invariant: root pyproject declares norecursedirs patterns that prune
every data-dir basename pytest could meet at the repo root.
"""
from __future__ import annotations

import sys
from fnmatch import fnmatch
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

# Basenames that must never be recursed into from the repo root.
DATA_DIR_BASENAMES = ["data", "data-selfimprove", "graphify-src", "web_dist"]


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "plugins").is_dir():
            return parent
    raise AssertionError("repo root with pyproject.toml + plugins/ not found")


def test_root_norecursedirs_prunes_data_dirs():
    root = _repo_root()
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    ini = config["tool"]["pytest"]["ini_options"]
    patterns = ini.get("norecursedirs", [])
    assert patterns, (
        "root pyproject must declare norecursedirs - without it pytest "
        "recurses into PRISM data dirs (stale repo mirrors) on explicit-arg runs"
    )
    for basename in DATA_DIR_BASENAMES:
        assert any(fnmatch(basename, pat) for pat in patterns), (
            f"norecursedirs does not prune '{basename}' - a data-dir repo "
            "mirror would be collected"
        )
