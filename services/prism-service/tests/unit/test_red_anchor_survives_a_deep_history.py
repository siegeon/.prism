"""The red anchor is found however far back the tests-only commit sits.

Task f97c196d, 2026-09-09. `_red_tests_commit` scanned `git log -n 80`, so
it could only find a tests-only commit within 80 commits of HEAD. That cap
measures the anchor's DISTANCE FROM HEAD, which grows every time anyone
commits to the repo. f97c196d's own tests-only commit (3c94a38e, "test(
conductor): pin the terminal reap node's safety contract [task:f97c196d]")
sat 201 commits back, so the scan missed it, the self-heal fell back to the
task worktree's HEAD -- 37fa182c, a commit belonging to a DIFFERENT task --
and red_gate refused the task with "NOT red: the spec's tests PASS at the
red-step commit 37fa182c5285". The task burned 2 of its 3 rewinds on it.

Raising the cap to 500 (commit 22bd4b27) only moves the cliff further out;
this suite pins the invariant that DEPTH DOES NOT MATTER, which is why the
scan now filters with `git log --grep <tag> --fixed-strings` instead.

RED at 22bd4b27: the tagged tests-only commit here is deliberately buried
deeper than every shipped cap (80 and 500).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services.conductor_service import ConductorService

TASK_ID = "f97c196d-356d-43da-bc6c-9489090379ba"
# Past BOTH caps this defect has worn: the original 80 and the 500 that
# replaced it in 22bd4b27. A fixture that only clears 80 would go green on
# the 500 and pin nothing -- the invariant under test is that NO depth cap
# governs the answer, so the burial must outrun the largest one shipped.
BURIAL_DEPTH = 520


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                       text=True)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout.strip()


@pytest.fixture()
def deep_repo(tmp_path):
    """A repo whose tests-only [task:...] commit is buried under many later
    commits, the shape a long-lived shared checkout always reaches."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")

    # The tests-only commit, trailered the way this repo documents.
    (repo / "tests" / "test_reap.py").write_text("def test_reap():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m",
         f"test(conductor): pin the reap contract\n\n[task:{TASK_ID[:8]}]\n")
    anchor = _git(repo, "rev-parse", "HEAD")

    # Bury it under unrelated work by other tasks. One shell loop of empty
    # commits: 520 separate `git` subprocesses from Python would dominate
    # this suite's runtime for no extra coverage.
    burial = subprocess.run(
        ["bash", "-c",
         f'for i in $(seq 1 {BURIAL_DEPTH}); do '
         f'git commit -q --allow-empty -m "chore: unrelated work $i" || exit 1; '
         f'done'],
        cwd=str(repo), capture_output=True, text=True)
    assert burial.returncode == 0, f"burial failed: {burial.stderr[-400:]}"

    return repo, anchor


def _conductor(repo: Path) -> ConductorService:
    svc = ConductorService(":memory:")
    svc._project_root = str(repo)
    # No task worktree in this fixture: fall straight through to the shared
    # checkout, which is the repo under test here.
    svc._workspace_and_head = lambda _task_id: ("", "")
    return svc


def test_the_anchor_is_found_however_deep_it_is_buried(deep_repo):
    repo, anchor = deep_repo

    sha, found_repo = _conductor(repo)._red_tests_commit(TASK_ID)

    depth = int(_git(repo, "rev-list", "--count", f"{anchor}..HEAD"))
    assert depth > 500, "fixture must bury the anchor past every shipped cap"
    assert sha == anchor, (
        f"tests-only commit {anchor[:8]} buried {depth} commits deep was not "
        f"found; got {sha[:8] if sha else '(nothing)'}")
    assert found_repo == str(repo)


def test_an_untagged_history_still_resolves_to_nothing(deep_repo):
    """The widened search must not start matching other tasks' commits."""
    repo, _anchor = deep_repo

    sha, _ = _conductor(repo)._red_tests_commit(
        "aaaaaaaa-0000-0000-0000-000000000000")

    assert sha == "", f"a task with no commits must resolve to '', got {sha}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
