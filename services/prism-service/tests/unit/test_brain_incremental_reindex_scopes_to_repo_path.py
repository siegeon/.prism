"""Brain.incremental_reindex(repo_path=...) must diff and index the GIVEN
project's own checkout, not whatever directory the daemon process happens
to be running in (task: livehang round 4).

Before this fix, `incremental_reindex()` ran `git diff`/`git ls-files`
with no `cwd` at all -- every project's periodic reindex actually diffed
the DAEMON'S OWN checkout, which is why nearly every one of ~30 tracked
projects on the live instance logged an identical "reindexed 65 drifted
file(s)" on every restart (the same one repo's diff, computed once per
project name) and why a genuinely unchanged project could never see its
own drift count settle to zero -- there was no real per-project baseline
to settle."""

from __future__ import annotations

import subprocess
from pathlib import Path

from prism_service.engines.brain_engine import Brain


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("print('hello from " + name + "')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _brain(tmp_path: Path, name: str) -> Brain:
    d = tmp_path / f"data-{name}"
    d.mkdir()
    return Brain(
        brain_db=str(d / "brain.db"), graph_db=str(d / "graph.db"),
        scores_db=str(d / "scores.db"), tasks_db=str(d / "tasks.db"),
    )


def test_reindex_diffs_the_given_repo_not_the_process_cwd(tmp_path, monkeypatch):
    repo_a = _repo(tmp_path, "repo-a")
    repo_b = _repo(tmp_path, "repo-b")
    # An untracked file in repo B only -- if incremental_reindex ignored
    # repo_path and used the process cwd instead, it would see whatever
    # happens to be dirty THERE (nothing, in this hermetic test), not this
    # file, and report 0 either way -- a false pass. Assert against BOTH
    # sides: repo B must see its own new file, repo A must see nothing.
    (repo_b / "new_file.py").write_text("x = 1\n")

    monkeypatch.chdir(tmp_path)  # process cwd is neither repo -- the old
    # code's failure mode reproduced: if repo_path were ignored, both
    # brains would diff THIS empty directory and both would report 0.

    brain_a = _brain(tmp_path, "a")
    brain_b = _brain(tmp_path, "b")

    n_a = brain_a.incremental_reindex(repo_path=str(repo_a))
    n_b = brain_b.incremental_reindex(repo_path=str(repo_b))

    assert n_a == 0, f"repo A has no changes of its own; got {n_a}"
    assert n_b == 1, (
        f"repo B's own untracked file must be found via git ls-files run "
        f"WITH cwd=repo_path, not the daemon's own working directory; "
        f"got {n_b}")


def test_second_pass_with_no_changes_reindexes_zero_files(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-c")
    monkeypatch.chdir(tmp_path)
    brain = _brain(tmp_path, "c")

    # First pass: the untracked file from repo creation... there is none
    # here (the repo is fully committed), so the FIRST pass should already
    # be zero -- proving the baseline starts clean, not "everything is
    # drift on the first run."
    first = brain.incremental_reindex(repo_path=str(repo))
    assert first == 0, f"a freshly-committed repo has no drift; got {first}"

    # A real change, then a real settle (committed, so `git diff HEAD`
    # naturally clears): the SAME repo, unchanged again, must report zero
    # a second time -- proving this project's own drift genuinely settles
    # instead of reporting a fixed, unrelated count forever (the live
    # symptom: every one of ~30 projects logging the SAME "65 drifted
    # files" indefinitely, because none of them were ever diffing their
    # OWN tree at all).
    (repo / "a.py").write_text("print('changed')\n")
    second = brain.incremental_reindex(repo_path=str(repo))
    assert second == 1, f"the one real change must be seen; got {second}"
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the change")

    third = brain.incremental_reindex(repo_path=str(repo))
    assert third == 0, (
        f"once the real change is committed, this project's own working "
        f"tree is clean again vs its own HEAD -- got {third}, which is "
        f"exactly the live symptom (every project reporting the same "
        f"non-zero count forever, because none of them was ever really "
        f"diffing its own tree)")
