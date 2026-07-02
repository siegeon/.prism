"""Shared location self-check for the test_implement_* guard suites.

The guard suites assert about their own home: they must target the canonical
.claude/workflows/implement.js, never a stray copy. The original check was a
blanket `"worktrees" not in path`, which also redded every legitimate agent
worktree (.claude/worktrees/<name>) of the canonical repo — the "5
pre-existing failures" caveat on every lane's full-suite receipt.

`worktree_of_canonical` decides by GIT IDENTITY instead (task 7faed505):

  * ACCEPT — a linked git worktree of the canonical repo: `git rev-parse`
    reports --git-dir != --git-common-dir (linked worktrees keep their git
    dir under <canonical>/.git/worktrees/<name>) AND the common repo's root
    hosts .claude/workflows/implement.js.
  * REJECT — a bare directory copy (no git identity: rev-parse fails, or
    ascends to an enclosing repo whose git-dir == common-dir).
  * REJECT — a genuinely foreign clone (its own .git: git-dir == common-dir).

Contract red-tested in tests/integration/test_worktree_aware_location_guard.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def worktree_of_canonical(workflows: Path) -> bool:
    """True iff `workflows` (<home>/.claude/workflows) sits inside a LINKED
    git worktree of the canonical repo. Never path-whitelists: the decision
    is git identity plus canonical-workflow membership."""
    home = workflows.parents[1]

    def _rev(flag: str) -> Path:
        out = subprocess.run(
            ["git", "-C", str(home), "rev-parse", "--path-format=absolute",
             flag],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return Path(out).resolve()

    try:
        git_dir = _rev("--git-dir")
        common = _rev("--git-common-dir")
    except (subprocess.CalledProcessError, OSError):
        return False  # no git identity at all — a bare copy
    if git_dir == common:
        # A main checkout or an independent clone — not a linked worktree.
        return False
    canonical_root = common.parent
    return (canonical_root / ".claude" / "workflows" / "implement.js").is_file()
