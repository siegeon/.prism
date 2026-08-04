"""Red tests for task 499ba9c9 -- "Shipping still registers after a squash
merge".

get_task_delivery's "merged" stage (services/prism-service/prism_service/api
/tasks.py:602-678) resolves purely by per-commit SHA ancestry
(`git merge-base --is-ancestor <sha> origin/main`, tasks.py:646). A squash
merge creates ONE brand-new commit on main that shares no SHA with the
task's own branch, so this false-negatives on exactly this repo's dominant
merge convention (every recent main commit is `(#NNN)`-suffixed -- a squash
merge). `_is_shipped_on_main` (tasks.py:278-293) already resolves
shipped-ness squash-safely by grepping commit MESSAGES on origin/main for
the delimited `[task:<id8>]` trailer -- built by task b22576bb for
`/stranded`, with a docstring naming THIS task as the one that wires the
same match into `get_task_delivery`.

The fix (implemented in the next step, NOT this one) is an additive
OR-fallback at the "merged" stage call site:
    merged_ok = _all("merged_to_main") or (bool(repo) and _is_shipped_on_main(repo, task_id))
so the existing SHA-ancestry fast path (true merge commits) is untouched
(NFR-1) and only genuinely-shipped-but-ancestry-invisible work (squash,
rebase) additionally resolves as merged.

Every scenario below runs against a DISPOSABLE fixture repo built in
tmp_path with a REAL `--bare` origin (git clone --bare + push + fetch) --
never a stubbed git layer, never a hand-built dict, never the real
E:\\.prism checkout.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from prism_service.api.tasks import get_task_delivery

# ---------------------------------------------------------------------------
# git fixture helpers (mirrors test_approve_finishes_delivery.py's /
# test_dashboard_unshipped_card.py's style)
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, check=True)
    return r.stdout.strip()


def _init_repo_with_bare_origin(tmp_path: Path) -> Path:
    """A working repo with commits pushed to a REAL `--bare` origin clone --
    `origin/main` is a genuine fetch-derived remote-tracking ref, not a
    hand-poked `update-ref`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _commit(repo: Path, path: str, body: str, subject: str) -> str:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", subject)
    return _git(repo, "rev-parse", "HEAD")


def _task(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=task_id, gate_state="", workflow_step="",
                           status="in_progress")


def _wire(monkeypatch, repo: Path, task_id: str, branch: str) -> None:
    """Patch the collaborators get_task_delivery reads: task_workspace for
    branch resolution, claude_transcripts for repo path, get_project for the
    task lookup -- the exact monkeypatch shape
    test_approve_finishes_delivery.py already validates."""
    from prism_service.services import task_workspace
    monkeypatch.setattr(
        task_workspace, "workspace_for",
        lambda tid: {"branch": branch, "path": str(repo), "repo_root": str(repo)}
        if tid == task_id else None)
    import prism_service.api.tasks as tasks_mod
    from prism_service.services import claude_transcripts as ct
    monkeypatch.setattr(ct, "_project_source_path", lambda project: str(repo))
    fake_task_svc = SimpleNamespace(get=lambda tid: _task(task_id))
    monkeypatch.setattr(tasks_mod, "get_project",
                        lambda project: SimpleNamespace(task_svc=fake_task_svc))


def _merged_stage(result: dict) -> dict:
    return next(s for s in result["stages"] if s["key"] == "merged")


# ---------------------------------------------------------------------------
# FR-1 / AC-1 -- a squash-merged branch (single new commit on main, no SHA
# shared with the branch) resolves 'merged' as merged via the trailer
# fallback.
# ---------------------------------------------------------------------------


def test_squash_merge_resolves_as_merged(tmp_path, monkeypatch):
    task_id = "sq112233"
    repo = _init_repo_with_bare_origin(tmp_path)
    branch = f"prism/ws/{task_id}"
    _git(repo, "checkout", "-qb", branch)
    tip = _commit(repo, "feature.txt", "work\n", f"add feature [task:{task_id}]")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--squash", branch)
    _git(repo, "commit", "-qm", f"add feature (#1) [task:{task_id[:8]}]")
    squash_sha = _git(repo, "rev-parse", "HEAD")
    assert squash_sha != tip

    anc = subprocess.run(["git", "merge-base", "--is-ancestor", tip, "main"],
                         cwd=str(repo))
    assert anc.returncode != 0, (
        "setup must reproduce a genuine squash: the branch tip must NOT be "
        "an ancestor of what landed on main")

    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    _wire(monkeypatch, repo, task_id, branch)

    result = get_task_delivery(task_id, project="prism")
    merged = _merged_stage(result)
    assert merged["state"] == "done", (
        f"a squash-merged task must resolve 'merged' as done: {result}")


# ---------------------------------------------------------------------------
# FR-2 / AC-2 -- a true merge commit (branch SHAs preserved as ancestors of
# main) still resolves 'merged' via the EXISTING SHA-ancestry fast path.
# NFR-1: this path must be provably untouched by the fix.
# ---------------------------------------------------------------------------


def test_true_merge_commit_still_resolves_via_ancestry(tmp_path, monkeypatch):
    task_id = "mg112233"
    repo = _init_repo_with_bare_origin(tmp_path)
    branch = f"prism/ws/{task_id}"
    _git(repo, "checkout", "-qb", branch)
    tip = _commit(repo, "feature.txt", "work\n", f"add feature [task:{task_id}]")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge the task branch", branch)
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")

    anc = subprocess.run(["git", "merge-base", "--is-ancestor", tip, "main"],
                         cwd=str(repo))
    assert anc.returncode == 0, (
        "setup: a real merge commit must preserve the branch tip as an "
        "ancestor of main")

    _wire(monkeypatch, repo, task_id, branch)
    result = get_task_delivery(task_id, project="prism")
    assert result["commits"], result
    assert all(c["merged_to_main"] for c in result["commits"]), (
        "the per-commit SHA-ancestry flag must resolve true via the "
        f"EXISTING merge-base --is-ancestor path, unmodified: {result['commits']}")
    assert _merged_stage(result)["state"] == "done", result


# ---------------------------------------------------------------------------
# FR-3 / AC-3 -- rebase-merge: the task's OWN workspace branch keeps its
# original SHAs (never itself rewritten -- exactly like real life, where a
# GitHub "rebase and merge" rewrites the PR's commits on the SERVER, not the
# contributor's local branch), while a MIRROR gets a real `git rebase` onto
# an advanced main and lands (ff) on origin/main with new SHAs but the same
# trailer. 'merged' must resolve via the trailer fallback.
# ---------------------------------------------------------------------------


def test_rebased_commits_resolve_as_merged(tmp_path, monkeypatch):
    task_id = "rb112233"
    repo = _init_repo_with_bare_origin(tmp_path)
    branch = f"prism/ws/{task_id}"
    _git(repo, "checkout", "-qb", branch)
    tip = _commit(repo, "feature.txt", "work\n", f"add feature [task:{task_id}]")

    _git(repo, "checkout", "-q", "main")
    _commit(repo, "other.txt", "unrelated\n", "advance main")
    _git(repo, "push", "-q", "origin", "main")

    _git(repo, "branch", "mirror", branch)
    _git(repo, "checkout", "-q", "mirror")
    _git(repo, "rebase", "-q", "main")
    rebased_tip = _git(repo, "rev-parse", "HEAD")
    assert rebased_tip != tip, "a real rebase onto an advanced main must yield a NEW sha"
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "mirror")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "checkout", "-q", branch)

    anc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tip, "origin/main"], cwd=str(repo))
    assert anc.returncode != 0, (
        "setup: the task's OWN (unrewritten) branch commit must NOT be an "
        "ancestor of origin/main -- otherwise this would test FR-2, not FR-3")

    _wire(monkeypatch, repo, task_id, branch)
    result = get_task_delivery(task_id, project="prism")
    assert result["commits"], result
    assert not all(c["merged_to_main"] for c in result["commits"]), (
        f"setup check: ancestry must genuinely fail for the unrewritten "
        f"branch commit: {result['commits']}")
    assert _merged_stage(result)["state"] == "done", (
        f"rebase-merged work (new SHAs, trailer preserved) must resolve "
        f"'merged' via the trailer fallback: {result}")


# ---------------------------------------------------------------------------
# FR-4 / AC-4 -- commits that exist only on an unmerged branch resolve
# 'merged' as NOT merged.
# ---------------------------------------------------------------------------


def test_unmerged_branch_resolves_as_not_merged(tmp_path, monkeypatch):
    task_id = "un112233"
    repo = _init_repo_with_bare_origin(tmp_path)
    branch = f"prism/ws/{task_id}"
    _git(repo, "checkout", "-qb", branch)
    _commit(repo, "feature.txt", "work\n", f"add feature [task:{task_id}]")
    _git(repo, "checkout", "-q", "main")  # main never sees the branch's work

    _wire(monkeypatch, repo, task_id, branch)
    result = get_task_delivery(task_id, project="prism")
    assert result["commits"], result
    assert _merged_stage(result)["state"] != "done", (
        f"a task whose commits exist only on an unmerged branch must NOT "
        f"resolve 'merged' as done: {result}")


# ---------------------------------------------------------------------------
# FR-5 / AC-5 -- a different task's trailer on main, and a bare/prefix
# substring occurrence of THIS task's id outside any [task:...] delimiter,
# must not false-positive this task's 'merged' stage.
# ---------------------------------------------------------------------------


def test_substring_and_other_task_mentions_do_not_false_positive(
    tmp_path, monkeypatch,
):
    task_id = "fp112233"
    other_id = "ff998877"
    repo = _init_repo_with_bare_origin(tmp_path)
    branch = f"prism/ws/{task_id}"
    _git(repo, "checkout", "-qb", branch)
    _commit(repo, "feature.txt", "work\n", f"add feature [task:{task_id}]")
    _git(repo, "checkout", "-q", "main")
    # a DIFFERENT task's real trailer lands on main...
    _commit(repo, "other.txt", "x\n", f"unrelated fix [task:{other_id}]")
    # ...and a commit mentioning THIS task's id as a bare/prefix-collision
    # substring, outside any [task:...] delimiter.
    _commit(repo, "note.txt", "y\n",
           f"a note mentioning task:{task_id}extra in prose")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")

    _wire(monkeypatch, repo, task_id, branch)
    result = get_task_delivery(task_id, project="prism")
    assert _merged_stage(result)["state"] != "done", (
        f"a different task's trailer plus a bare substring mention of this "
        f"task's id must NOT cause 'merged' to false-positive: {result}")
