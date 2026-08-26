"""Red tests for task bb1d934e-6ee4-4b2b-8e91-9703cbd0a6d1's own delivery
card lying: `get_task_delivery`'s "pushed"/"released" stages
(services/prism-service/prism_service/api/tasks.py) were computed purely
from the task's OWN tracked branch commits (`_delivery_git_facts`'
`git log --grep "task:<id8>" <branch>`).

A PRISM-on-PRISM self-dev direct-land (see CLAUDE.md's "Key Conventions"
carve-out) re-commits the same `[task:<id8>]`-trailered content directly to
origin/main under a FRESH sha -- the task's ORIGINAL tracked worktree
branch commit is left behind, local-only, forever unpushed. `merged_ok`
already had an OR-fallback onto `_is_shipped_on_main` (task 499ba9c9,
squash-merge case) so "merged" correctly shows done -- but "pushed" and
"released" had no equivalent fallback, so the live task page showed
'merged to main' checked while the headline banner still read "not yet
delivered -- branch not pushed", contradicting itself. Fixed by adding a
`shipped_sha` alongside the existing `shipped_on_main` bool
(`_shipped_sha_on_main`) and OR-ing "pushed"/"released" onto facts about
THAT sha, exactly mirroring the existing "merged" OR-fallback.

Same disposable, real `--bare` origin fixture style as
test_delivery_detects_squash_merge.py -- never a stubbed git layer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from prism_service.api.tasks import get_task_delivery

# ---------------------------------------------------------------------------
# fixture helpers (mirrors test_delivery_detects_squash_merge.py)
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, check=True)
    return r.stdout.strip()


def _init_repo_with_bare_origin(tmp_path: Path) -> Path:
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
                           status="done")


def _wire(monkeypatch, repo: Path, task_id: str, branch: str) -> None:
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


def _stage(result: dict, key: str) -> dict:
    return next(s for s in result["stages"] if s["key"] == key)


# ---------------------------------------------------------------------------
# The real scenario: the tracked worktree commit never reaches origin, but
# a distinct direct-land commit carrying the same trailer does.
# ---------------------------------------------------------------------------


def test_self_dev_direct_land_resolves_pushed_via_shipped_sha(tmp_path, monkeypatch):
    task_id = "sd112233"
    repo = _init_repo_with_bare_origin(tmp_path)
    branch = f"prism/ws/{task_id}"
    # The task's own tracked branch commit -- created, never pushed anywhere.
    _git(repo, "checkout", "-qb", branch)
    _commit(repo, "feature.txt", "work\n", f"test: pin invariant [task:{task_id}]")
    _git(repo, "checkout", "-q", "main")

    # A SEPARATE direct commit, same trailer, lands straight on origin/main
    # (the self-dev convention) -- no ancestry relationship to the branch.
    _commit(repo, "feature.txt", "work\n", f"land directly [task:{task_id[:8]}]")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")

    _wire(monkeypatch, repo, task_id, branch)
    result = get_task_delivery(task_id, project="prism")

    assert result["commits"], result
    assert not any(c["pushed"] for c in result["commits"]), (
        "setup check: the tracked branch commit must genuinely be "
        f"unpushed, or this test doesn't reproduce the bug: {result['commits']}")
    assert _stage(result, "merged")["state"] == "done", (
        f"the existing OR-fallback must already resolve 'merged': {result}")
    assert _stage(result, "pushed")["state"] == "done", (
        "'pushed' must resolve done once a shipped sha carrying this "
        f"task's trailer is found on origin/main: {result}")


def test_self_dev_direct_land_resolves_released_via_tag_on_shipped_sha(
    tmp_path, monkeypatch,
):
    task_id = "sd445566"
    repo = _init_repo_with_bare_origin(tmp_path)
    branch = f"prism/ws/{task_id}"
    _git(repo, "checkout", "-qb", branch)
    _commit(repo, "feature.txt", "work\n", f"test: pin invariant [task:{task_id}]")
    _git(repo, "checkout", "-q", "main")

    _commit(repo, "feature.txt", "work\n", f"land directly [task:{task_id[:8]}]")
    _git(repo, "tag", "v9.9.9")
    _git(repo, "push", "-q", "origin", "main", "--tags")
    _git(repo, "fetch", "-q", "origin")

    _wire(monkeypatch, repo, task_id, branch)
    result = get_task_delivery(task_id, project="prism")

    released = _stage(result, "released")
    assert released["state"] == "done", (
        f"a release tag containing the shipped sha must resolve "
        f"'released' as done: {result}")
    assert released["detail"] == "v9.9.9", released


# ---------------------------------------------------------------------------
# Guard: no shipped sha anywhere -> pushed/released must stay NOT done. The
# OR-fallback must not become an unconditional pass.
# ---------------------------------------------------------------------------


def test_genuinely_unshipped_task_still_shows_not_pushed_not_released(
    tmp_path, monkeypatch,
):
    task_id = "ns778899"
    repo = _init_repo_with_bare_origin(tmp_path)
    branch = f"prism/ws/{task_id}"
    _git(repo, "checkout", "-qb", branch)
    _commit(repo, "feature.txt", "work\n", f"test: pin invariant [task:{task_id}]")
    _git(repo, "checkout", "-q", "main")  # main never sees this task at all

    _wire(monkeypatch, repo, task_id, branch)
    result = get_task_delivery(task_id, project="prism")

    assert _stage(result, "pushed")["state"] != "done", result
    assert _stage(result, "merged")["state"] != "done", result
    assert _stage(result, "released")["state"] != "done", result
    assert result["delivered"] is False, result
