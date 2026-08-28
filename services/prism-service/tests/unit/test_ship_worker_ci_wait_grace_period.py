"""Task 811fcce0: ci_wait's grace window for "no checks reported yet".

DEFECT this suite pins: ship_worker's ci_wait stage treated "no checks
reported on the 'main' branch" as an immediate hard failure right after
push + pr_create, when GitHub simply had not registered the PR's checks
yet -- a false negative on the very first poll. Landing task 292e8ea2
needed three separate ship_task runs before it passed ci_wait, and task
8bcd4cb3 hit the identical pattern (2026-08-27).

  AC (task oracle): ci_wait does NOT report a final failure on its first
  poll when the response is exactly "no checks reported on the '<base>'
  branch" -- it waits a bounded grace window before treating that
  specific case as failed. Any OTHER ci_wait failure (a real check that
  ran and failed) still fails immediately on the first poll, unchanged --
  the ticket's own named likely_misfire is a blanket retry across every
  failure, which would mask a genuinely failed check behind an
  unnecessary wait.

Same fixture shape as test_ship_worker.py / test_ship_worker_rebase.py:
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

TASK_ID = "811fcce0-0000-0000-0000-000000000000"


def _git(cwd, *args) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _unshipped_workspace(tmp_path: Path, task_id: str = TASK_ID):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", str(origin))

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _write(work, "README.md", "# baseline\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "baseline")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "origin", "main")

    branch = f"prism/ws/{task_id}"
    _git(work, "checkout", "-q", "-b", branch)
    _write(work, "services/prism-service/prism_service/services/ship_worker.py",
           "# the change under test\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm",
         f"fix: ci_wait grace period\n\n[task:{task_id[:8]}]")
    return origin, work, branch


class FakeGh:
    """Same seam as test_ship_worker.py's FakeGh, extended so `pr checks`
    can answer "no checks reported" for its first `not_yet_calls` calls
    before succeeding -- or forever, when `not_yet_calls` is None."""

    def __init__(self, origin: Path, branch: str, *,
                 not_yet_calls: int | None = 0,
                 not_yet_branch: str = "main",
                 fail_at: str = "", fail_err: str = ""):
        self.origin = origin
        self.branch = branch
        self.calls: list[list] = []
        self.checks_calls = 0
        self.not_yet_calls = not_yet_calls
        self.not_yet_branch = not_yet_branch
        self.fail_at = fail_at
        self.fail_err = fail_err

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
            return 0, "https://github.com/siegeon/.prism/pull/42\n", ""
        if "pr checks" in head:
            self.checks_calls += 1
            if self.fail_at == "ci_wait":
                return 1, "", self.fail_err
            if (self.not_yet_calls is None
                    or self.checks_calls <= self.not_yet_calls):
                return (1, "",
                        f"no checks reported on the '{self.not_yet_branch}' "
                        "branch")
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
        if "auth status" in head:
            return 0, "Logged in to github.com as siegeon\n", ""
        return 0, "", ""


def _wire_ws(monkeypatch, work: Path, branch: str, task_id: str = TASK_ID):
    import prism_service.services.task_workspace as tw
    rec = {"task_id": task_id, "path": str(work), "branch": branch,
           "repo_root": str(work)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: dict(rec))
    monkeypatch.setattr(tw, "workspace_record", lambda tid: dict(rec),
                        raising=False)


def _shipped(repo: Path, task_id: str = TASK_ID) -> bool:
    from prism_service.api.tasks import _is_shipped_on_main
    _git(repo, "fetch", "-q", "origin")
    return _is_shipped_on_main(str(repo), task_id)


# ---------------------------------------------------------------------------
# The pinned AC — task.verify names this exact test.
# ---------------------------------------------------------------------------


def test_no_checks_reported_gets_a_grace_window_before_failing(
        tmp_path, monkeypatch):
    """"No checks reported" on the first couple of polls does NOT fail
    ci_wait immediately -- once GitHub actually registers the checks
    (here, on the 3rd poll) the pipeline completes normally."""
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)
    assert not _shipped(work), "fixture must start UNshipped"

    gh = FakeGh(origin, branch, not_yet_calls=2)
    res = ship_worker.ship_task(
        TASK_ID, runner=gh, poll_interval_s=0,
        not_yet_registered_grace_s=5.0)

    assert res["ok"] is True, res
    assert gh.checks_calls == 3, (
        "expected two 'no checks reported' polls before the third "
        f"succeeds, got {gh.checks_calls} polls")
    assert _shipped(work), "the pipeline must still land after the grace window"


# ---------------------------------------------------------------------------
# Companion coverage: the grace window is BOUNDED, and a real check
# failure is unaffected (still fails on the first poll).
# ---------------------------------------------------------------------------


def test_no_checks_reported_forever_still_fails_after_the_grace_window(
        tmp_path, monkeypatch):
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)

    gh = FakeGh(origin, branch, not_yet_calls=None)  # never registers
    res = ship_worker.ship_task(
        TASK_ID, runner=gh, poll_interval_s=0,
        not_yet_registered_grace_s=0.05)

    assert res["ok"] is False, res
    assert res["stage"] == "ci_wait", res
    assert "no checks" in res["error"].lower(), res
    assert not _shipped(work), "an exhausted grace window must not land anything"


def test_a_genuine_check_failure_still_fails_on_the_first_poll(
        tmp_path, monkeypatch):
    """The ticket's own likely_misfire: a REAL check failure must not be
    masked behind the new grace window -- it fails immediately, exactly
    as before this fix, with zero retries."""
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)

    gh = FakeGh(origin, branch, fail_at="ci_wait",
               fail_err="check 'build' failed: exit 1")
    res = ship_worker.ship_task(
        TASK_ID, runner=gh, poll_interval_s=0,
        not_yet_registered_grace_s=5.0)

    assert res["ok"] is False, res
    assert res["stage"] == "ci_wait", res
    assert "check 'build' failed: exit 1" in res["error"], res
    assert gh.checks_calls == 1, (
        "a real check failure must fail on its FIRST poll, not retry "
        f"through the grace window: {gh.checks_calls} polls")
    assert not _shipped(work)
