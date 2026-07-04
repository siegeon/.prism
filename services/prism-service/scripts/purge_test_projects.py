#!/usr/bin/env python
"""Purge junk TEST projects that leaked into a PRISM data dir.

Before v6.7.25 the test suite had no PRISM_DATA_DIR isolation, so
API/seam tests wrote throwaway projects (named after pytest tmp_path
basenames, probes, etc.) into the live store. This removes them.

SAFETY MODEL (repo destructive-ops doctrine):
  * DRY-RUN by default — nothing is deleted without an explicit --yes.
  * --data-dir must exist and contain a projects/ subdir, else exit 2.
  * Only immediate children of <data-dir>/projects are ever considered.
  * A candidate must LOOK like a project dir (mulch/ / workflow/ /
    tasks.db / scores.db / understand_state.json) or be empty-ish;
    anything else is refused, never force-deleted.
  * 'default' and 'prism' are hard-protected regardless of patterns.
  * Symlinks are refused (never followed into a real store).
  * Errors during deletion are REPORTED, never swallowed.

Usage:
  python scripts/purge_test_projects.py --data-dir E:/path/to/data --dry-run
  python scripts/purge_test_projects.py --data-dir E:/path/to/data --yes
"""

from __future__ import annotations

import argparse
import fnmatch
import shutil
import sys
from pathlib import Path

# Junk slugs observed in the live store audit (2026-07-03) + the shapes
# the un-isolated suite generates (pytest tmp_path basenames, probes).
JUNK_PATTERNS: tuple[str, ...] = (
    "test_*",        # pytest tmp_path basenames: test_conductor_state_api_carri0 ...
    "test-*",        # test-onboard, ...
    "*_pid",         # done_tile_pid, per_step_eta_pid, ...
    "probe",
    "perf_probe",
    "proj",
    "project-a",
    "project-b",
    "search-test",
    "bh-demo*",
    # Additional machine-generated shapes observed in the default
    # %LOCALAPPDATA%\prism store (also suite leakage, older vintages):
    "explicit-create-*",
    "onboard-probe-*",
    "phantom-probe-*",
    "proj-*",          # proj-x, proj-z (tests/unit/test_claude_run_log.py)
    "test-ll-*",       # test-ll-09 (tests/unit/test_mcp_augmentation.py)
)

# Never delete these, even if a pattern somehow matches.
PROTECTED: frozenset[str] = frozenset({"default", "prism"})

# Files/dirs whose presence marks a directory as a PRISM project dir.
_PROJECT_MARKERS: tuple[str, ...] = (
    "mulch", "workflow", "graph", "source",
    "understand_state.json", "tasks.db", "scores.db",
)


def _matches_junk(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in JUNK_PATTERNS)


def _looks_like_project_dir(p: Path) -> bool:
    """True if p carries any PRISM project marker, or is entirely empty
    (a bare leaked mkdir). Anything else is NOT ours to delete."""
    try:
        entries = list(p.iterdir())
    except OSError as e:
        print(f"  ! cannot inspect {p}: {e}", file=sys.stderr)
        return False
    if not entries:
        return True
    return any((p / m).exists() for m in _PROJECT_MARKERS)


def _fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def _validate_projects_root(data_dir: Path) -> Path:
    if not data_dir.exists():
        _fail(f"--data-dir does not exist: {data_dir}")
    if not data_dir.is_dir():
        _fail(f"--data-dir is not a directory: {data_dir}")
    projects = data_dir / "projects"
    if not projects.is_dir():
        _fail(
            f"{data_dir} has no projects/ subdir — refusing: this does "
            "not look like a PRISM data dir"
        )
    return projects


def collect_candidates(projects: Path) -> tuple[list[Path], list[Path]]:
    """Return (deletable, refused) junk-named children of projects/."""
    deletable: list[Path] = []
    refused: list[Path] = []
    for child in sorted(projects.iterdir()):
        name = child.name
        if name in PROTECTED or not _matches_junk(name):
            continue
        if child.is_symlink():
            print(f"  ! refusing symlink: {child}", file=sys.stderr)
            refused.append(child)
            continue
        if not child.is_dir():
            print(f"  ! refusing non-directory: {child}", file=sys.stderr)
            refused.append(child)
            continue
        if child.resolve().parent != projects.resolve():
            print(f"  ! refusing path outside projects/: {child}",
                  file=sys.stderr)
            refused.append(child)
            continue
        if not _looks_like_project_dir(child):
            print(f"  ! refusing (no project markers, not empty): {child}",
                  file=sys.stderr)
            refused.append(child)
            continue
        deletable.append(child)
    return deletable, refused


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Purge junk test projects from a PRISM data dir "
                    "(dry-run by default).")
    ap.add_argument("--data-dir", required=True,
                    help="PRISM data dir (the parent of projects/)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be removed (this is the DEFAULT)")
    ap.add_argument("--yes", action="store_true",
                    help="actually delete (required for any removal)")
    args = ap.parse_args(argv)

    if args.dry_run and args.yes:
        _fail("--dry-run and --yes are mutually exclusive")

    projects = _validate_projects_root(Path(args.data_dir).resolve())
    deletable, refused = collect_candidates(projects)

    if not deletable and not refused:
        print(f"nothing junk-named under {projects}")
        return 0

    mode = "DELETE" if args.yes else "DRY-RUN (pass --yes to delete)"
    print(f"{mode} — {len(deletable)} junk project(s) under {projects}:")
    for p in deletable:
        print(f"  - {p.name}")

    if refused:
        print(f"refused {len(refused)} junk-named entr(y/ies) that do not "
              "look like project dirs (see stderr) — remove manually if "
              "truly junk.")

    if not args.yes:
        return 0

    failures = 0
    for p in deletable:
        try:
            shutil.rmtree(p)  # errors are raised + reported, never ignored
            print(f"  removed {p.name}")
        except OSError as e:
            failures += 1
            print(f"  ! FAILED to remove {p}: {e}", file=sys.stderr)
    if failures:
        print(f"{failures} removal(s) failed — see stderr.", file=sys.stderr)
        return 1
    print(f"done: removed {len(deletable)} project dir(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
