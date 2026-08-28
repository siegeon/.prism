"""ci_wait must trust the STATUS text over a magic pending exit code.

DEFECT: GH_CHECKS_PENDING_RC=8 is the documented gh CLI exit code for "a
check is still pending", but this machine's gh 2.4.0 (2022) returns rc=1
for the identical, completely normal pending state, with stdout reading
exactly "checks\tpending\t0\t<url>" and an empty stderr. Every real poll
during a normal CI run therefore fell into the "any OTHER ci_wait
failure" branch and failed on its FIRST poll -- ci_wait's own retry loop
never ran even once. Observed live: task dd2b87c8 and 1bc0b316 each got
blocked by ship_worker's 3-consecutive-identical-failure circuit breaker
while their PRs' CI was genuinely still running and later passed cleanly.

AC: a `gh pr checks` reply whose exit code is NOT GH_CHECKS_PENDING_RC but
whose text contains a pending-like STATUS token (pending/queued/
in_progress/waiting) is treated as "still pending, keep polling", not an
immediate hard failure -- and a genuine check failure (a real FAIL status,
no pending token anywhere in the text) still fails immediately on its
first poll, unchanged.

Same fixture shape as test_ship_worker_ci_wait_grace_period.py: git runs
for real against a real bare origin; only `gh` is stubbed.
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

TASK_ID = "sw-pend-0000-0000-0000-000000000000"


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
         f"fix: pending status text\n\n[task:{task_id[:8]}]")
    return origin, work, branch


class FakeGh:
    """`pr checks` answers with the REAL rc=1 / "checks\\tpending\\t0\\t<url>"
    shape observed live for `pending_calls` polls, then succeeds -- or
    answers a genuine, non-pending failure when `fail_text` is set."""

    def __init__(self, origin: Path, branch: str, *,
                 pending_calls: int = 0, fail_text: str = ""):
        self.origin = origin
        self.branch = branch
        self.checks_calls = 0
        self.pending_calls = pending_calls
        self.fail_text = fail_text

    def __call__(self, argv, cwd=None):
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
            if self.fail_text:
                return 1, self.fail_text, ""
            if self.checks_calls <= self.pending_calls:
                return (1,
                        "checks\tpending\t0\t"
                        "https://github.com/siegeon/.prism/actions/runs/1/"
                        "job/1\n", "")
            return 0, "checks\tpass\t8m9s\thttps://x\n", ""
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


def test_pending_status_text_with_a_non_documented_rc_keeps_polling(
        tmp_path, monkeypatch):
    """The real live shape: rc=1 (not the documented 8), stdout says
    "pending" -- ci_wait must keep polling instead of failing on the
    first poll."""
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)
    assert not _shipped(work), "fixture must start UNshipped"

    gh = FakeGh(origin, branch, pending_calls=3)
    res = ship_worker.ship_task(TASK_ID, runner=gh, poll_interval_s=0)

    assert res["ok"] is True, res
    assert gh.checks_calls == 4, (
        f"expected 3 pending polls then a passing 4th, got {gh.checks_calls}")
    assert _shipped(work), "the pipeline must land once checks actually pass"


def test_a_genuine_failure_with_no_pending_token_still_fails_on_first_poll(
        tmp_path, monkeypatch):
    """A real check failure (no pending/queued/in_progress/waiting token
    anywhere in the text) must not be swallowed by the new tolerance --
    it still fails immediately, exactly as before this fix."""
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)

    gh = FakeGh(origin, branch, fail_text="checks\tfail\t1m2s\thttps://x\n")
    res = ship_worker.ship_task(TASK_ID, runner=gh, poll_interval_s=0)

    assert res["ok"] is False, res
    assert res["stage"] == "ci_wait", res
    assert gh.checks_calls == 1, "a real failure must not be retried"
    assert not _shipped(work), "a genuine check failure must not land anything"
