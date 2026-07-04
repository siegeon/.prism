#!/usr/bin/env python
"""Purge junk TEST projects that leaked into a PRISM data dir.

Before v6.7.25 the test suite had no PRISM_DATA_DIR isolation, so
API/seam tests wrote throwaway projects (named after pytest tmp_path
basenames, probes, etc.) into the live store. This removes them.

SAFETY MODEL (repo destructive-ops doctrine):
  * DRY-RUN by default — nothing is deleted without an explicit --yes.
  * --data-dir must exist and contain a projects/ subdir, else exit 2.
  * Only immediate children of <data-dir>/projects are ever considered.
  * Junk shapes are PRECISE (exact names + anchored regexes) — no broad
    wildcards that could swallow a real project slug.
  * A name match alone never deletes: a junk-CONFIRMING heuristic must
    also hold (no content-rich brain.db, no mulch/expertise/*.jsonl
    memories, no populated source/ checkout). Content-rich projects are
    refused LOUDLY even when junk-named.
  * default / prism / bh-demo / test-onboard / talentsync / think-shift
    are hard-protected regardless of shape.
  * Symlinks are refused (never followed into a real store).
  * Errors during deletion are REPORTED, never swallowed.

Usage:
  python scripts/purge_test_projects.py --data-dir E:/path/to/data --dry-run
  python scripts/purge_test_projects.py --data-dir E:/path/to/data --yes
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

# PRECISE junk shapes only. Broad wildcards ("test-*", "bh-demo*") are
# BANNED: the 2026-07-03 review found they matched REAL live projects
# (bh-demo: 20MB brain.db + source, updated Jul 2; test-onboard: 21MB
# brain.db + real mulch/expertise memories).
#
# The pytest tmp_path leak names are test_<test fn truncated to 30 chars>
# plus a trailing digit, always snake_case — target exactly that shape.
JUNK_NAME_REGEXES: tuple[re.Pattern, ...] = (
    re.compile(r"^test_[a-z0-9_]{1,29}\d$"),        # pytest tmp_path slugs
    re.compile(r"^explicit-create-[0-9a-f]{12}$"),  # legacy suite probes
    re.compile(r"^onboard-probe-[0-9a-f]{12}$"),
    re.compile(r"^phantom-probe-[0-9a-f]{12}$"),
)

# Explicit audit list (live-store audit 2026-07-03) — exact names only.
JUNK_EXACT: frozenset[str] = frozenset({
    "done_tile_pid", "per_step_eta_pid", "perf_probe", "probe", "proj",
    "project-a", "project-b", "search-test",
    "proj-x", "proj-z",  # tests/unit/test_claude_run_log.py
    "test-ll-09",        # tests/unit/test_mcp_augmentation.py
})

# Never delete these, even if a shape somehow matches. bh-demo and
# test-onboard LOOK junk-named but are real, content-rich projects.
PROTECTED: frozenset[str] = frozenset({
    "default", "prism", "bh-demo", "test-onboard", "talentsync", "think-shift",
})

# Content-richness veto threshold: a leaked test project's brain.db (if it
# even has one) stays tiny; a real project's runs to megabytes.
_LEAK_BRAIN_MAX_BYTES = 256 * 1024


def _matches_junk(name: str) -> bool:
    return name in JUNK_EXACT or any(
        rx.match(name) for rx in JUNK_NAME_REGEXES
    )


def _plausible_test_leak(p: Path) -> tuple[bool, str]:
    """(True, "") only when p is plausibly a LEAKED TEST project.

    Junk-CONFIRMING heuristic — the earlier marker-existence check was
    inverted as a safety gate (every real project carries the markers
    too). A name match alone is not enough: anything content-rich VETOES
    deletion, with the reason returned for a loud refusal message."""
    try:
        brain = p / "brain.db"
        if brain.is_file() and brain.stat().st_size >= _LEAK_BRAIN_MAX_BYTES:
            return False, (
                f"brain.db is {brain.stat().st_size:,} bytes (content-rich)")
        expertise = p / "mulch" / "expertise"
        if expertise.is_dir() and any(expertise.glob("*.jsonl")):
            return False, "has mulch/expertise/*.jsonl (real memories)"
        source = p / "source"
        if source.is_dir() and any(source.iterdir()):
            return False, "source/ is non-empty (real checkout)"
    except OSError as e:
        return False, f"cannot inspect: {e}"
    return True, ""


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
        leak, reason = _plausible_test_leak(child)
        if not leak:
            print(f"  ! REFUSING {name}: junk-shaped name but looks REAL "
                  f"({reason}) — remove manually only if truly junk",
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
        print(f"refused {len(refused)} junk-named entr(y/ies) that look "
              "REAL or could not be safely confirmed (see stderr).")

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
