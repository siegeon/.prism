"""Shipped-ness means the task's OWN commits reached origin/main (task
9db4f4c8) — a foreign commit that merely borrows the `[task:<id8>]`
trailer does not count while the task's real branch sits unlanded.

LIVE REGRESSIONS this pins, both identical in shape:
- 8bcd4cb3 (2026-08-27): a session hand-committed a one-line fix on main
  with the task's trailer; the daemon then produced the real work on
  prism/ws/8bcd4cb3. The shipped-ness tooth saw the trailer, fail-opened,
  the close skipped ship_worker, the branch stranded, DONE was false.
- a2bc8c88 (2026-09-04): same again — a hand commit's trailer on main
  closed the task while the drive's name-set fix sat local and unpushed.

The guard: when a trailer IS found on origin/main, the tooth also checks
the task's own `prism/ws/<task_id>` branch via `git cherry origin/main`
(patch-id equivalence, so a squash-landed branch still reads as
represented). Real, non-empty-diff '+' commits mean the borrowed trailer
is not this task's ship: the tooth refuses with the branch tip named, the
gate parks, and ship_worker lands the branch.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

TASK_ID = "9db4f4c8-231f-40ad-9267-a03052d75ed6"


def _git(cwd, *args) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path):
    """origin + workspace clone with a baseline main, a real task branch
    (one genuine commit), and a FOREIGN hand commit on main that borrows
    the task's trailer — the exact live shape of both regressions."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", str(origin))

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _write(work, "README.md", "# baseline\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "baseline")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "origin", "main")

    branch = f"prism/ws/{TASK_ID}"
    _git(work, "checkout", "-q", "-b", branch)
    _write(work, "real_work.txt", "the drive's actual fix\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm",
         f"fix: the drive's real work\n\n[task:{TASK_ID[:8]}]\n")

    _git(work, "checkout", "-q", "main")
    _write(work, "hand_fix.txt", "a different, hand-made change\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm",
         f"fix: hand commit that borrows the trailer [task:{TASK_ID[:8]}]\n")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")
    return origin, work, branch


def _tooth(work: Path, monkeypatch) -> str:
    from prism_service.services import task_workspace
    from prism_service.services.conductor_service import ConductorService
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(work)})
    svc = ConductorService.__new__(ConductorService)
    return svc._unshipped_gate_reason(SimpleNamespace(id=TASK_ID))


def test_a_borrowed_trailer_on_main_does_not_close_an_unlanded_branch(
        tmp_path, monkeypatch):
    """The regression itself: trailer on main, real branch unlanded —
    the tooth must REFUSE and name the branch tip, never fail-open."""
    _origin, work, branch = _repo(tmp_path)
    tip = _git(work, "rev-parse", branch)

    reason = _tooth(work, monkeypatch)

    assert reason, ("must refuse: the trailer commit on main is not this "
                    "task's own work and the real branch is unlanded")
    assert tip[:12] in reason, "the refusal names the stranded branch tip"
    assert "ship_worker" in reason


def test_a_merged_branch_passes(tmp_path, monkeypatch):
    """Once the task's own branch is genuinely merged into origin/main,
    the tooth has no objection."""
    _origin, work, branch = _repo(tmp_path)
    _git(work, "merge", "-q", "--no-ff", "-m", "land", branch)
    _git(work, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")

    assert _tooth(work, monkeypatch) == ""


def test_a_squash_represented_branch_passes(tmp_path, monkeypatch):
    """A squash-landed branch shares no ancestry with main, but its patch
    is on main — `git cherry` reads it as represented and the tooth has
    no objection (the a205eb7a squash-safety contract, kept)."""
    _origin, work, branch = _repo(tmp_path)
    # replay the branch's patch as a NEW commit on main (a squash land)
    _git(work, "cherry-pick", "--no-commit", branch)
    _git(work, "commit", "-qm",
         f"ship: squash land [task:{TASK_ID[:8]}]\n")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")

    assert _tooth(work, monkeypatch) == ""
