"""The conductor pipeline REAPS what a finished task leaves (task f97c196d).

WORKFLOW_STEPS ends at green_gate and the behavior FSM ends at `land`, so
nothing ever removed a drive's git worktree or its branch. Measured on this
repo 2026-08-30: 256 worktrees and 474 branches, 352 of them `prism/ws/*`.

The reap node is the FSM's step AFTER `land` -- it runs on a SUCCESSFUL
land, not on `status == done`, because the branch is only provably
disposable once it is really on origin/main.

SHIPPEDNESS IS THE TASK TRAILER, NEVER `merge-base --is-ancestor`. Of 48
branches that failed the is-ancestor test on this repo, 47 had fully
shipped under a squash/PR sha. `test_a_shipped_task_loses_its_worktree_and_
branch` builds exactly that shape: the landed commit is a SIBLING carrying
a DIFFERENT patch, so is-ancestor says "not shipped", `git cherry` says
"unique commit", and only reading the `[task:<id8>]` trailer on origin/main
lets the reap proceed.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from prism_service.services import task_reaper, task_workspace

TASK = "f97c196d-1111-2222-3333-444444444444"


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {r.stderr}")
    return r.stdout.strip()


def _rc(cwd: Path, *args: str) -> int:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=60).returncode


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git repo with one commit on main, an origin/main ref, and an
    isolated data dir -- a mocked git proves nothing for a deletion tool."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "base")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(task_workspace, "resolve_data_dir",
                        lambda: tmp_path / "data")
    return root


def _worktree(repo: Path, task_id: str = TASK) -> Path:
    rec = task_workspace.ensure_workspace(task_id, repo_root=str(repo))
    return Path(rec["path"])


def _commit_in(ws: Path, text: str, message: str) -> None:
    (ws / "f.txt").write_text(text, encoding="utf-8")
    _git(ws, "add", "f.txt")
    _git(ws, "commit", "-qm", message)


def _land_a_squash(repo: Path, task_id: str = TASK, text: str = "work\nfix\n") -> str:
    """The real shape a PR squash-merge leaves: a commit on origin/main that
    carries the task trailer but is a SIBLING of the branch tip, with its
    own patch. is-ancestor cannot see it; the trailer can."""
    (repo / "f.txt").write_text(text, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", f"feat: the thing (#900) [task:{task_id[:8]}]")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return _git(repo, "rev-parse", "HEAD")


def _branches(repo: Path) -> list[str]:
    return _git(repo, "branch", "--format=%(refname:short)").splitlines()


def _worktree_paths(repo: Path) -> str:
    return _git(repo, "worktree", "list")


# --------------------------------------------------------------------------
# The two pinned tests (task.verify)
# --------------------------------------------------------------------------
def test_a_shipped_task_loses_its_worktree_and_branch(repo: Path) -> None:
    ws = _worktree(repo)
    _commit_in(ws, "work\n", f"feat: the thing [task:{TASK[:8]}]")
    branch = f"prism/ws/{TASK}"
    _land_a_squash(repo)

    # The scenario is real: is-ancestor REFUSES this branch, and `git cherry`
    # calls its commit unique, because the landed patch differs.
    assert _rc(repo, "merge-base", "--is-ancestor", branch, "origin/main") != 0
    assert [ln for ln in _git(ws, "cherry", "origin/main", "HEAD").splitlines()
            if ln.startswith("+")]

    verdict = task_reaper.reap_task(TASK, status="done", repo_root=str(repo))

    assert verdict["reaped"] is True, verdict["reason"]
    assert verdict["outcome"] == "pass"
    assert verdict["shipped_sha"]
    assert verdict["worktree_removed"] is True
    assert verdict["branch_deleted"] is True
    assert not ws.exists()
    assert branch not in _branches(repo)
    assert str(ws) not in _worktree_paths(repo)


def test_a_dirty_worktree_is_never_reaped(repo: Path) -> None:
    ws = _worktree(repo)
    _commit_in(ws, "work\n", f"feat: the thing [task:{TASK[:8]}]")
    _land_a_squash(repo)
    # Uncommitted work sitting in the checkout -- the one thing a reap can
    # destroy that nothing else holds a copy of.
    (ws / "unsaved.txt").write_text("not committed anywhere\n", encoding="utf-8")

    verdict = task_reaper.reap_task(TASK, status="done", repo_root=str(repo))

    assert verdict["reaped"] is False
    assert verdict["outcome"] == "refused"
    assert "uncommitted" in verdict["reason"].lower()
    assert ws.exists()
    assert (ws / "unsaved.txt").exists()
    assert f"prism/ws/{TASK}" in _branches(repo)
    assert str(ws) in _worktree_paths(repo)


# --------------------------------------------------------------------------
# The rest of the safety contract
# --------------------------------------------------------------------------
def test_a_branch_whose_commits_are_nowhere_else_survives(repo: Path) -> None:
    """No trailer on origin/main and a unique commit: the branch is the ONLY
    copy of that work, so the reap refuses rather than `branch -D` it."""
    ws = _worktree(repo)
    _commit_in(ws, "work\n", "feat: never landed")

    verdict = task_reaper.reap_task(TASK, status="done", repo_root=str(repo))

    assert verdict["reaped"] is False
    assert verdict["shipped_sha"] == ""
    assert "commit" in verdict["reason"].lower()
    assert ws.exists()
    assert f"prism/ws/{TASK}" in _branches(repo)


def test_an_unfinished_task_is_never_reaped(repo: Path) -> None:
    ws = _worktree(repo)
    _commit_in(ws, "work\n", f"feat: the thing [task:{TASK[:8]}]")
    _land_a_squash(repo)

    for status in ("pending", "in_progress", ""):
        verdict = task_reaper.reap_task(TASK, status=status, repo_root=str(repo))
        assert verdict["reaped"] is False, status
        assert "finished" in verdict["reason"].lower()
        assert ws.exists()


def test_a_live_drive_keeps_its_worktree(repo: Path) -> None:
    """Rule 4: never touch a worktree a drive is standing in right now."""
    ws = _worktree(repo)
    _commit_in(ws, "work\n", f"feat: the thing [task:{TASK[:8]}]")
    _land_a_squash(repo)

    verdict = task_reaper.reap_task(TASK, status="done", repo_root=str(repo),
                                    is_live=lambda tid: True)

    assert verdict["reaped"] is False
    assert "live" in verdict["reason"].lower()
    assert ws.exists()


def test_the_main_checkout_is_never_reaped(repo: Path) -> None:
    """A workspace record pointing at the shared checkout must be refused
    before any `worktree remove` runs."""
    task_workspace.ensure_workspace(TASK, repo_root=str(repo))
    idx = task_workspace._load_index()
    idx[TASK]["path"] = str(repo)
    task_workspace._save_index(idx)
    _land_a_squash(repo)

    verdict = task_reaper.reap_task(TASK, status="done", repo_root=str(repo))

    assert verdict["reaped"] is False
    assert "checkout" in verdict["reason"].lower()
    assert (repo / "a.txt").exists()


def test_a_shipped_orphan_branch_is_reaped_without_a_worktree(repo: Path) -> None:
    """127 of the 154 orphan `prism/ws/*` branches held work already on main
    and had no worktree left. The node collects those too."""
    ws = _worktree(repo)
    _commit_in(ws, "work\n", f"feat: the thing [task:{TASK[:8]}]")
    _land_a_squash(repo)
    branch = f"prism/ws/{TASK}"
    # Tear the checkout down the way a hand cleanup does, leaving the branch.
    _git(repo, "worktree", "remove", "--force", str(ws))
    idx = task_workspace._load_index()
    idx.pop(TASK, None)
    task_workspace._save_index(idx)
    assert branch in _branches(repo)

    verdict = task_reaper.reap_task(TASK, status="done", repo_root=str(repo))

    assert verdict["reaped"] is True, verdict["reason"]
    assert verdict["branch_deleted"] is True
    assert branch not in _branches(repo)


def test_the_reap_never_asks_git_whether_a_branch_is_an_ancestor() -> None:
    """stop_if: 'the reap cannot prove shippedness without is-ancestor'."""
    source = Path(task_reaper.__file__).read_text(encoding="utf-8")
    assert "is-ancestor" not in source
    assert "merge_base" not in source.replace("merge_base(", "")
    # It reuses the gate's OWN squash-safe reader, never a second copy.
    assert "_shipped_sha_on_main" in source
    assert "def _shipped_sha_on_main" not in source


# --------------------------------------------------------------------------
# The node is on the pipeline, and it is CODIFIED
# --------------------------------------------------------------------------
def _behavior(name: str) -> dict:
    root = Path(__file__).resolve().parents[4]
    return json.loads((root / ".prism" / "behaviors" / "conductor" /
                       f"{name}.json").read_text(encoding="utf-8"))


def test_reap_is_the_conductors_terminal_node_after_land() -> None:
    bot = _behavior("bot")
    ids = bot["fsms"][0]["behaviorIds"]
    assert ids[-2:] == ["land", "reap"], ids

    reap = _behavior("reap")
    assert reap["fsmId"] == "pipeline" and reap["botId"] == "conductor"
    assert [s["id"] for s in reap["steps"]] == ["survey", "reap"]

    from prism_service.services import flow_run_recorder as rec
    assert rec.CONDUCTOR_NODES[-3:] == ("green_gate", "land", rec.REAP_NODE)
    assert rec.REAP_NODE == "reap"
    # A walk that reached reap is finished, exactly as one that reached land.
    assert rec.is_finished([{"node_id": rec.REAP_NODE}]) is True
    assert rec.is_finished([{"node_id": "land"}]) is True

    from prism_service.api import workflows as wf
    assert "reap" in wf._BEHAVIOR_TRIGGER
    assert "land" in wf._BEHAVIOR_TRIGGER["reap"].lower()


def test_the_reap_node_is_codified_and_calls_no_model() -> None:
    """Only /steps/reason-loop and /steps/premise-judge are agentic."""
    reap = _behavior("reap")
    for step in reap["steps"]:
        assert step["kind"] == "http-callback"
        assert "/api/workflows/steps/reap" in step["url"]
        assert "model" not in step["body"]
    # The inference package is the ONE way this service calls a model
    # (api/workflows.py's /steps/premise-judge imports
    # prism_service.inference.claude_cli). Neither the reaper nor the route
    # that serves it reaches for it.
    reaper = Path(task_reaper.__file__).read_text(encoding="utf-8")
    assert "prism_service.inference" not in reaper

    from prism_service.api import workflows as wf
    route = Path(wf.__file__).read_text(encoding="utf-8")
    handler = route[route.index("def workflow_step_reap("):]
    assert "prism_service.inference" not in handler


def test_the_survey_step_deletes_nothing(repo: Path) -> None:
    ws = _worktree(repo)
    _commit_in(ws, "work\n", f"feat: the thing [task:{TASK[:8]}]")
    _land_a_squash(repo)

    verdict = task_reaper.reap_task(TASK, status="done", repo_root=str(repo),
                                    mode="survey")

    assert verdict["outcome"] == "pass"
    assert verdict["reaped"] is False
    assert verdict["would_reap"] is True
    assert ws.exists()
    assert f"prism/ws/{TASK}" in _branches(repo)
