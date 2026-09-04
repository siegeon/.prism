"""`_fresh_diff_base` must adopt the fresh merge-base when the worktree's
stored baseline sits on a DIVERGED lineage (task ce471e06, 2026-09-04).

`workspace_for`'s `baseline` is stamped once at worktree creation and never
updated. `_fresh_diff_base` exists to self-heal that, preferring
merge-base(HEAD, origin/main) — but it adopted the fresh point ONLY when
the stored baseline was an ancestor of it, via a single
`git merge-base --is-ancestor stale fresh` probe. That probe exits
non-zero for "behind" AND for "diverged" alike, and `_git` maps every
non-zero exit to None, so a diverged baseline fell through to the stale
value: the exact false positive the self-heal was written to prevent.

LIVE REGRESSION: this repo's self-dev carve-out commits to both `dev` and
`main`, so task ce471e06's workspace carried a baseline on the dev
lineage. Once `ship_worker` rebased its branch onto origin/main, neither
commit was an ancestor of the other. The stale dev baseline was kept, the
first-parent walk absorbed main's own commits, and
`candidate_policy_edits` blamed the UI-only candidate for a
`conductor_service.py` change it never made — the machine gate seat
abstained ("candidate modified gate policy") and the task could not
finish.

All three relationships are pinned here, because the fix must not loosen
the guard it narrows:
  * FORWARD (stale is an ancestor of fresh)  -> adopt fresh
  * BACKWARD (fresh is an ancestor of stale) -> keep stale
  * DIVERGED (neither)                       -> adopt fresh
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _git(args, cwd) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)}
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _commit(work: Path, name: str, body: str) -> str:
    (work / name).write_text(body, encoding="utf-8")
    _git(["add", name], work)
    _git(["commit", "-q", "-m", f"add {name}"], work)
    return _git(["rev-parse", "HEAD"], work)


def _repo(tmp_path: Path):
    """origin + clone with a seeded main, returning (origin, work, seed)."""
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], tmp_path)
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], origin)

    work = tmp_path / "work"
    _git(["clone", "-q", str(origin), str(work)], tmp_path)
    seed = _commit(work, "README.md", "seed\n")
    _git(["branch", "-M", "main"], work)
    _git(["push", "-q", "origin", "HEAD:main"], work)
    _git(["fetch", "-q", "origin"], work)
    return origin, work, seed


def test_diverged_lineage_adopts_the_fresh_merge_base(tmp_path):
    """The live ce471e06 shape: the stored baseline is on one lineage, the
    branch has been rebased onto another, neither is an ancestor of the
    other. The fresh merge-base must win — keeping the stale one is what
    misattributed main's own commits to the candidate."""
    from prism_service.services import control_plane as cp

    _origin, work, seed = _repo(tmp_path)

    # A "dev" lineage: the workspace baseline is stamped here.
    _git(["checkout", "-q", "-b", "devline"], work)
    dev_baseline = _commit(work, "dev_only.txt", "dev side\n")

    # main moves on independently (another task lands work).
    _git(["checkout", "-q", "main"], work)
    main_tip = _commit(work, "main_only.txt", "main side\n")
    _git(["push", "-q", "origin", "HEAD:main"], work)
    _git(["fetch", "-q", "origin"], work)

    # The task branch now sits on main (as ship_worker's rebase leaves it).
    _git(["checkout", "-q", "-b", "prism/ws/task"], work)
    _commit(work, "candidate.txt", "the candidate's own change\n")

    # Sanity: the stored baseline and the fresh point genuinely diverged.
    for a, b in ((dev_baseline, main_tip), (main_tip, dev_baseline)):
        r = subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                           cwd=str(work), capture_output=True)
        assert r.returncode != 0, "fixture must produce DIVERGED lineages"

    got = cp._fresh_diff_base(work, dev_baseline)

    assert got == main_tip, (
        "a diverged stored baseline must yield to merge-base(HEAD, "
        f"origin/main); got {got!r}, wanted {main_tip!r}")


def test_forward_motion_still_adopts_the_fresh_base(tmp_path):
    """Unchanged behaviour: a stored baseline that is an ancestor of the
    fresh point is stale-but-on-lineage, and the fresh point wins."""
    from prism_service.services import control_plane as cp

    _origin, work, seed = _repo(tmp_path)
    main_tip = _commit(work, "later.txt", "later\n")
    _git(["push", "-q", "origin", "HEAD:main"], work)
    _git(["fetch", "-q", "origin"], work)

    assert cp._fresh_diff_base(work, seed) == main_tip


def test_fresh_base_behind_the_stored_baseline_keeps_the_stored_one(tmp_path):
    """Unchanged guard: when the fresh point is an ANCESTOR of the stored
    baseline (a worktree created off a local commit not yet pushed), the
    stored baseline is kept so the diff base never walks backward over
    real, unrelated local commits."""
    from prism_service.services import control_plane as cp

    _origin, work, seed = _repo(tmp_path)
    # A local, unpushed commit becomes this worktree's baseline; origin/main
    # still points at the seed, so merge-base(HEAD, origin/main) == seed,
    # which is BEHIND the stored baseline.
    local_baseline = _commit(work, "unpushed.txt", "not pushed yet\n")

    assert cp._fresh_diff_base(work, local_baseline) == local_baseline


def test_missing_origin_ref_falls_back_to_the_stored_baseline(tmp_path):
    """No origin/main to resolve (no remote) keeps today's behaviour."""
    from prism_service.services import control_plane as cp

    work = tmp_path / "solo"
    work.mkdir()
    _git(["init", "-q", str(work)], tmp_path)
    base = _commit(work, "a.txt", "a\n")

    assert cp._fresh_diff_base(work, base) == base
