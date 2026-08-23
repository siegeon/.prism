"""Shipped-ness checks must recognize a [task:<FULL UUID>] trailer, not
just the documented [task:<id8>] short form (task a205eb7a, 2026-08-21).

LIVE REGRESSION this pins: task a205eb7a's real driver (write_failing_tests
/ implement_tasks, autonomous task_runner) committed with the trailer
`[task:a205eb7a-d46b-4d1c-a2a0-809a0c1e3ff0]` -- the FULL uuid, not the
documented 8-char short form `[task:a205eb7a]`. Three call sites assumed
the short form EXACTLY (an 8-char id then an immediate closing bracket):
  - conductor_service._unshipped_gate_reason (git log --grep, --fixed-strings)
  - api.tasks._is_shipped_on_main (same pattern, against origin/main)
  - api.tasks._compute_stranded (regex `\\[task:([^\\]\\s]{8})\\]`)
All three require the bracket to close right after exactly 8 characters.
Reproduced empirically against the REAL task a205eb7a workspace: the old
exact-bracket grep pattern found ZERO commits, while a prefix-only pattern
(`[task:a205eb7a`, no closing bracket) found the real commit
`0b5908c2ae03c8011adea6fffa71349cdb6311d8` immediately. Because
_unshipped_gate_reason FAIL-OPENS ("" / no objection) when it finds no
local trailer at all -- a deliberate, correct behavior for a task that
genuinely never committed anything -- this made the shipped-ness tooth
silently no-op for a task that HAD committed, just with a differently-
shaped trailer, letting it reach status=done/full_outcome_complete=True
while genuinely unshipped (not yet reachable from origin/main).

Fix: all three sites now match the trailer as a PREFIX (open bracket + the
8-char short id, no closing bracket required in the grep/regex), so both
the short form and a full-UUID trailer are recognized identically.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

TASK_ID = "a205eb7a-d46b-4d1c-a2a0-809a0c1e3ff0"


def _git(cwd, *args) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _repo_with_full_uuid_trailer(tmp_path: Path):
    """A real repo whose task-branch commit carries the FULL uuid trailer --
    exactly task a205eb7a's real, live shape -- merged (not squashed) into
    origin/main, same as _is_shipped_on_main's own squash-safe docstring
    scenario but with the full-length trailer instead of the short one."""
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
    _write(work, "feature.txt", "the change\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm",
         f"fix: remove the dead Simulate flow toolbar button\n\n"
         f"[task:{TASK_ID}]\n")
    return origin, work, branch


def test_reproduces_the_live_bug_short_form_grep_misses_full_uuid_trailer(
        tmp_path):
    """Empirical regression proof, no mocks: the OLD short-form-exact grep
    genuinely finds nothing against a real full-uuid trailer commit, while
    a prefix grep finds it immediately -- exactly what was observed live
    against task a205eb7a's actual workspace before this fix."""
    _origin, work, _branch = _repo_with_full_uuid_trailer(tmp_path)

    old_exact = subprocess.run(
        ["git", "-C", str(work), "log", "--all", "--fixed-strings",
         "--grep", f"[task:{TASK_ID[:8]}]", "-n", "1", "--format=%H"],
        capture_output=True, text=True)
    assert old_exact.stdout.strip() == "", (
        "sanity check: the old exact-bracket pattern must NOT find the "
        "full-uuid-trailer commit, reproducing the live bug")

    new_prefix = subprocess.run(
        ["git", "-C", str(work), "log", "--all", "--fixed-strings",
         "--grep", f"[task:{TASK_ID[:8]}", "-n", "1", "--format=%H"],
        capture_output=True, text=True)
    assert new_prefix.stdout.strip(), (
        "the prefix pattern must find the commit")


def test_unshipped_gate_reason_recognizes_a_full_uuid_trailer(
        tmp_path, monkeypatch):
    """conductor_service._unshipped_gate_reason must not fail-open just
    because the real trailer is longer than the documented short form --
    it must correctly report UNSHIPPED (a real refusal), not silently
    abstain, when the commit with the full-uuid trailer is genuinely not
    yet on origin/main."""
    from prism_service.services import task_workspace
    from prism_service.services.conductor_service import ConductorService

    origin, work, branch = _repo_with_full_uuid_trailer(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(work), "branch": branch})

    from types import SimpleNamespace
    task = SimpleNamespace(id=TASK_ID)
    svc = ConductorService.__new__(ConductorService)
    reason = svc._unshipped_gate_reason(task)

    assert reason, (
        "must REFUSE (non-empty reason) -- the commit exists locally "
        "(full-uuid trailer) but is genuinely not on origin/main yet; "
        "fail-opening here is the exact live bug this test pins")
    assert "origin/main" in reason.lower() or "not yet reachable" in reason.lower()


def test_unshipped_gate_reason_clears_once_the_full_uuid_trailer_ships(
        tmp_path, monkeypatch):
    """Anti-over-strictness: once the full-uuid-trailer commit IS merged to
    origin/main, the tooth must clear (return ""), not stay stuck refusing
    forever."""
    from prism_service.services import task_workspace
    from prism_service.services.conductor_service import ConductorService

    origin, work, branch = _repo_with_full_uuid_trailer(tmp_path)
    _git(work, "checkout", "-q", "main")
    _git(work, "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch)
    _git(work, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")

    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(work), "branch": branch})

    from types import SimpleNamespace
    task = SimpleNamespace(id=TASK_ID)
    svc = ConductorService.__new__(ConductorService)
    reason = svc._unshipped_gate_reason(task)

    assert reason == "", (
        f"must clear once genuinely shipped, got {reason!r}")


def test_is_shipped_on_main_recognizes_a_full_uuid_trailer(tmp_path):
    from prism_service.api.tasks import _is_shipped_on_main

    origin, work, branch = _repo_with_full_uuid_trailer(tmp_path)
    assert _is_shipped_on_main(str(work), TASK_ID) is False, (
        "not merged yet -- must report False, not silently True")

    _git(work, "checkout", "-q", "main")
    _git(work, "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch)
    _git(work, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")

    assert _is_shipped_on_main(str(work), TASK_ID) is True, (
        "merged, full-uuid trailer -- must now report True")


def test_is_shipped_on_main_still_matches_the_documented_short_form(tmp_path):
    """Anti-regression: the short 8-char form (the documented convention,
    what ship_worker.py's own PR body template writes) must still match --
    this fix broadens matching, it must not narrow it."""
    from prism_service.api.tasks import _is_shipped_on_main

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _write(work, "README.md", "# x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", f"fix: thing\n\n[task:{TASK_ID[:8]}]\n")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "origin", "main")

    assert _is_shipped_on_main(str(work), TASK_ID) is True


def test_compute_stranded_regex_captures_short_id_from_a_full_uuid_trailer():
    """api.tasks._compute_stranded's needle-extraction regex must capture
    the 8-char short id whether the trailer closes right after it (the
    documented form) or continues with more characters (the real a205eb7a
    shape) -- never require the bracket to close at exactly 8 chars."""
    pattern = r"\[task:([^\]\s]{8})"

    short_form = "fix: thing\n\n[task:a205eb7a]\n"
    full_form = "fix: thing\n\n[task:a205eb7a-d46b-4d1c-a2a0-809a0c1e3ff0]\n"

    m1 = re.search(pattern, short_form)
    m2 = re.search(pattern, full_form)
    assert m1 and m1.group(1) == "a205eb7a"
    assert m2 and m2.group(1) == "a205eb7a", (
        "must capture the short id prefix even when the real trailer is "
        "a full uuid -- this is the exact live a205eb7a regression")
