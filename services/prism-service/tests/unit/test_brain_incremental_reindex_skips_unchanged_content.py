"""Brain.incremental_reindex must not re-embed a file whose CONTENT is
unchanged, even though `git diff HEAD` names it every single pass as long
as it stays uncommitted (task: livehang round 6).

Round 4 fixed WHICH project's checkout got diffed (repo_path scoping).
That alone still left a real, sustained cost: a file edited once and then
left uncommitted -- the normal state of active development -- is a "git
diff HEAD" candidate on EVERY pass, forever, and the old code re-parsed
and re-embedded it every single time regardless of whether its content
had actually changed since the previous pass. Observed live: drift_
reindex passes of 3-9s back to back every ~5s, all re-embedding the
identical 72 dirty files of an actively-worked checkout, ~147% sustained
CPU.

Fix: each candidate's content is sha256-hashed and compared against a
hash persisted in this Brain's own index_meta table. A candidate whose
hash still matches the last pass that actually indexed it is skipped
entirely -- no _remove_entries_by_source, no _index_files, no re-embed."""

from __future__ import annotations

import subprocess
import time
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
    (repo / "a.py").write_text("print('hello')\n")
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


def test_second_pass_over_an_unchanged_dirty_file_embeds_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    monkeypatch.chdir(tmp_path)
    brain = _brain(tmp_path, "a")

    # Make the file genuinely DIRTY (uncommitted) -- this is what stays a
    # `git diff HEAD` candidate on every pass, committed or not.
    (repo / "a.py").write_text("print('edited once')\n")

    stats1: dict = {}
    n1 = brain.incremental_reindex(repo_path=str(repo), stats=stats1)
    assert n1 == 1, f"the one real edit must be embedded on the first pass; got {n1}"
    assert stats1["candidates"] == 1, f"got {stats1!r}"
    assert stats1["changed"] == 1, f"got {stats1!r}"

    # Second pass: the SAME file is STILL a git-diff candidate (still
    # uncommitted), but its CONTENT has not changed since the first pass
    # actually embedded it.
    t0 = time.monotonic()
    stats2: dict = {}
    n2 = brain.incremental_reindex(repo_path=str(repo), stats=stats2)
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    assert stats2["candidates"] == 1, (
        f"the file is still uncommitted, so git diff must still name it "
        f"as a candidate; got {stats2!r}")
    assert n2 == 0 and stats2["changed"] == 0 and stats2["embedded"] == 0, (
        f"the file's CONTENT has not changed since the last real embed -- "
        f"it must be skipped entirely (0 changed, 0 embedded), not "
        f"re-parsed and re-embedded just because it is still uncommitted; "
        f"got n={n2}, stats={stats2!r}")
    assert elapsed_ms < 100, (
        f"a pass that embeds zero files must be fast (no re-parse, no "
        f"re-embed work at all) -- got {elapsed_ms:.1f}ms")


def test_a_real_content_change_is_still_picked_up(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-b")
    monkeypatch.chdir(tmp_path)
    brain = _brain(tmp_path, "b")

    (repo / "a.py").write_text("print('first edit')\n")
    brain.incremental_reindex(repo_path=str(repo))  # settle the baseline

    # A REAL second edit -- content actually differs now.
    (repo / "a.py").write_text("print('second, different edit')\n")
    stats: dict = {}
    n = brain.incremental_reindex(repo_path=str(repo), stats=stats)
    assert n == 1 and stats["changed"] == 1 and stats["embedded"] == 1, (
        f"a genuine content change must still be re-embedded even though "
        f"the file was ALREADY a candidate on the previous pass too; "
        f"got n={n}, stats={stats!r}")
