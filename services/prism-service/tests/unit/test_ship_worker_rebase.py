"""ship_worker rebases a task branch onto current origin/main before push
(task 229954e4).

REAL, CONFIRMED-LIVE DEFECT this suite pins: `ship_task` pushed a task's
own branch AS-IS with no rebase attempt first. Four real tasks on
2026-08-26 (0e2c82f3, 82cc05ee, 85f92e4b, 8b4e7cb6) were all cut from an
origin/main that had since moved forward a dozen+ commits -- GitHub could
not compute a clean merge base, `mergeable` read CONFLICTING/UNKNOWN, and
`gh pr checks` reported "no checks reported" forever (a stale-base
symptom, not a real CI failure). Manually fetching origin/main and
rebasing flipped the PR to MERGEABLE/CLEAN and real checks started.

  AC(a)  a branch that is BEHIND origin/main but has NO real conflict with
         it gets rebased and pushed cleanly -- origin/main ends up an
         ancestor of what lands.
  AC(b)  a branch with a GENUINE conflicting change is never force-pushed
         with commits dropped -- the original commits stay reachable, the
         worktree is left exactly as it was (never mid-rebase), and the
         reported failure names the conflicting file so the stall-guard's
         blocked_reason gives a human something to act on. Per this
         ticket's own likely_misfire, the rebase must NEVER silently
         auto-resolve real content conflicts -- not even two branches both
         bumping the same version line.
  AC(c)  a branch already up to date with origin/main is a no-op fast
         path: no `git rebase` is even attempted.

Same fixture shape as test_ship_worker.py / test_ship_worker_machine_track.py:
git runs for real against a real bare origin; only `gh` is stubbed.
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

TASK_ID = "229954e4-0000-0000-0000-000000000000"


def _git(cwd, *args) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _unshipped_workspace(tmp_path: Path, task_id: str = TASK_ID,
                         feature_rel: str = "feature.txt",
                         feature_text: str = "the change under test\n"):
    """A real bare `origin` + a work checkout whose `[task:<id8>]` commit
    sits on the task's own branch, unpushed -- same shape as
    test_ship_worker.py's fixture."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", str(origin))
    # Force the bare origin's default branch to 'main' regardless of the
    # runner's own git config (init.defaultBranch). Without this,
    # _advance_origin_main's SECOND clone of `origin` checks out whatever
    # HEAD's symref names -- 'main' only on a box whose global git config
    # sets init.defaultBranch=main (masks this locally); a bare CI runner's
    # built-in default ('master') doesn't exist on this repo, so that clone
    # lands on an unborn branch and its later `git push origin main` fails
    # with "src refspec main does not match any" (rc=1) -- the exact
    # CI-only failure this fixture hit (task 8b4e7cb6, run 33045863069).
    # Same fix already applied once in
    # test_green_gate_requires_reachability.py (7.13.100).
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _write(work, "README.md", "# baseline\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "baseline")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "origin", "main")

    branch = f"prism/ws/{task_id}"
    _git(work, "checkout", "-q", "-b", branch)
    _write(work, feature_rel, feature_text)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm",
         f"feat: task branch change\n\n[task:{task_id[:8]}]")
    return origin, work, branch


def _advance_origin_main(tmp_path: Path, origin: Path, rel: str, text: str,
                         msg: str = "advance main") -> str:
    """Land a real commit on origin's main via a throwaway clone -- exactly
    the shape of "someone else pushed to main while this task's branch sat
    around": the task's own branch, cut earlier, knows nothing about it."""
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _write(other, rel, text)
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", msg)
    _git(other, "push", "-q", "origin", "main")
    return _git(other, "rev-parse", "HEAD")


class FakeGh:
    """Records every argv; runs `git` FOR REAL, answers only `gh` -- same
    shape as test_ship_worker.py's FakeGh (a real squash-style merge)."""

    def __init__(self, origin: Path, branch: str):
        self.origin = origin
        self.branch = branch
        self.calls: list[list] = []

    def __call__(self, argv, cwd=None):
        self.calls.append(list(argv))
        head = " ".join(str(a) for a in argv[:3])

        if argv[0] == "git":
            proc = subprocess.run(
                [str(a) for a in argv], cwd=str(cwd) if cwd else None,
                capture_output=True, text=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "t",
                     "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                     "GIT_COMMITTER_EMAIL": "t@t"})
            return proc.returncode, proc.stdout or "", proc.stderr or ""

        if argv[0] != "gh":
            raise AssertionError(f"pipeline shelled out to {argv[0]!r}: {argv}")

        if "pr create" in head:
            return 0, "https://github.com/siegeon/.prism/pull/99\n", ""
        if "pr checks" in head:
            return 0, "all checks passed\n", ""
        if "pr merge" in head:
            tmp = self.origin.parent / "merger"
            if not tmp.exists():
                _git(self.origin.parent, "clone", "-q", str(self.origin),
                     str(tmp))
            _git(tmp, "fetch", "-q", "origin", f"{self.branch}:{self.branch}")
            _git(tmp, "checkout", "-q", "main")
            _git(tmp, "merge", "-q", "--no-ff", "-m",
                 f"merge {self.branch}", self.branch)
            _git(tmp, "push", "-q", "origin", "main")
            return 0, "merged\n", ""
        return 0, "", ""

    def rebase_calls(self) -> list[list]:
        return [c for c in self.calls if c[0] == "git" and "rebase" in c]

    def push_calls(self) -> list[list]:
        return [c for c in self.calls if c[0] == "git" and c[1:2] == ["push"]]


def _shipped(repo: Path, task_id: str = TASK_ID) -> bool:
    from prism_service.api.tasks import _is_shipped_on_main
    _git(repo, "fetch", "-q", "origin")
    return _is_shipped_on_main(str(repo), task_id)


def _wire_ws(monkeypatch, work: Path, branch: str, task_id: str = TASK_ID):
    import prism_service.services.task_workspace as tw
    rec = {"task_id": task_id, "path": str(work), "branch": branch,
           "repo_root": str(work)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: dict(rec))
    monkeypatch.setattr(tw, "workspace_record", lambda tid: dict(rec),
                        raising=False)


# ---------------------------------------------------------------------------
# AC(a) -- behind origin/main, no real conflict: rebases and pushes cleanly
# ---------------------------------------------------------------------------


def test_a_branch_behind_main_with_no_conflict_rebases_and_ships(
        tmp_path, monkeypatch):
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(
        tmp_path, feature_rel="feature.txt", feature_text="task change\n")
    # Someone else pushed to origin/main after this branch was cut, touching
    # a DIFFERENT file -- a stale base, but no content overlap.
    main_sha = _advance_origin_main(
        tmp_path, origin, "OTHER.txt", "unrelated main change\n")
    _wire_ws(monkeypatch, work, branch)
    assert not _shipped(work), "fixture must start UNshipped"

    gh = FakeGh(origin, branch)
    res = ship_worker.ship_task(TASK_ID, runner=gh, poll_interval_s=0)

    assert res["ok"] is True, res
    assert gh.rebase_calls(), (
        "a genuinely-behind branch must actually be rebased, not pushed as-is")
    assert any("--force-with-lease" in c for c in gh.push_calls()), (
        "a rebase rewrites shas -- the push must use --force-with-lease, "
        f"never a bare force: {gh.push_calls()!r}")

    assert _shipped(work), "the rebased branch must still land on origin/main"
    # origin/main's pre-existing tip is now an ancestor of what shipped --
    # proof the rebase actually replayed onto it, not just pushed around it.
    _git(work, "fetch", "-q", "origin")
    is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", main_sha, "origin/main"],
        cwd=str(work), capture_output=True).returncode
    assert is_ancestor == 0, (
        "origin/main's advanced tip must be an ancestor of the final "
        "origin/main after landing")


# ---------------------------------------------------------------------------
# AC(b) -- a genuine conflict never drops commits and names the file
# ---------------------------------------------------------------------------


def test_a_genuine_conflict_aborts_cleanly_and_names_the_file(
        tmp_path, monkeypatch):
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(
        tmp_path, feature_rel="VERSION.txt", feature_text="7.13.999-task\n")
    original_head = _git(work, "rev-parse", "HEAD")
    # Someone else's push ALSO bumps the same line of the same file -- the
    # exact "two branches both bump PRISM_VERSION" case the likely_misfire
    # warns against silently auto-resolving.
    _advance_origin_main(tmp_path, origin, "VERSION.txt", "7.13.999-main\n")
    _wire_ws(monkeypatch, work, branch)
    assert not _shipped(work), "fixture must start UNshipped"

    gh = FakeGh(origin, branch)
    res = ship_worker.ship_task(TASK_ID, runner=gh, poll_interval_s=0)

    assert res["ok"] is False, res
    assert res["stage"] == "rebase", res
    assert "VERSION.txt" in res["error"], (
        f"the failure must name the conflicting file: {res['error']!r}")
    assert "manual resolution" in res["error"].lower(), res["error"]

    # Never force-pushed with commits dropped.
    assert not _shipped(work), "a conflicted rebase must never have landed"
    assert not gh.push_calls(), (
        "a rebase conflict must be caught BEFORE any push is attempted")

    # The worktree is left exactly as it was: clean, not mid-rebase, HEAD
    # back on the branch's original commit.
    assert not (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()
    assert _git(work, "status", "--porcelain") == "", (
        "the worktree must be clean after an aborted rebase")
    assert _git(work, "rev-parse", "HEAD") == original_head, (
        "HEAD must be back on the branch's original commit -- no commits "
        "silently dropped")
    assert _git(work, "rev-parse", branch) == original_head, (
        "the task's own branch ref must still point at its original, "
        "unmodified commit -- fully recoverable")


# ---------------------------------------------------------------------------
# AC(c) -- already up to date: no-op fast path, no rebase attempted
# ---------------------------------------------------------------------------


def test_a_branch_already_current_skips_rebase_entirely(
        tmp_path, monkeypatch):
    from prism_service.services import ship_worker

    # No _advance_origin_main call at all -- origin/main is exactly the
    # commit this branch was cut from.
    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)
    assert not _shipped(work), "fixture must start UNshipped"

    gh = FakeGh(origin, branch)
    res = ship_worker.ship_task(TASK_ID, runner=gh, poll_interval_s=0)

    assert res["ok"] is True, res
    assert not gh.rebase_calls(), (
        "an already-current branch must never attempt a rebase -- wasted "
        f"work: {gh.calls!r}")
    assert not any("--force-with-lease" in c for c in gh.push_calls()), (
        "no rebase happened, so the push must be a plain push, never a "
        f"force: {gh.push_calls()!r}")
    assert _shipped(work)
