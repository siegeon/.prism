"""Approving green_gate finishes the pipeline (task cb1dc6f4).

Owner 2026-07-29, after approving every gate and still finding features
absent: "i approved the green gate ... make sure to fix prism so that when
approved we finish the pipeline there." Approving green_gate used to mark a
task verified and STOP — nothing merged, so the board said done while a
task's code sat on its own branch forever (measured 2026-07-29: 19 of 137
prism/ws/* branches carried commits that never reached main).

Two parts, covered here:

  PART 1 (prerequisite) — GET /api/tasks/<id>/delivery resolved the shared
  checkout's CURRENT branch instead of the task's OWN branch, so it reported
  "no commits found" for tasks that plainly had tagged commits on their own
  workspace branch. `_resolve_delivery_branch` fixes this.

  PART 2 — ConductorService.deliver_task lands a task's own branch on local
  main: re-verifies the pinned tests AT the tree being merged (never trusts
  a stale receipt — the likely_misfire this session itself produced: a
  receipt at e58a6df, a follow-up commit 767aa63 on top), refuses on red,
  parks on conflict, never forces/resets/rewrites main, and confirms
  delivery from git facts (ancestry), never a command's exit code alone.

Every merge scenario below runs against a DISPOSABLE throwaway git repo
built in tmp_path — never against the real E:\\.prism checkout.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from prism_service.api.tasks import get_task_delivery
from prism_service.services.conductor_service import ConductorService

# `_resolve_delivery_branch` (api/tasks.py) and `_delivery_enabled`
# (conductor_service.py) are the NEW symbols this task adds. Importing them
# lazily, inside each test that needs them, keeps a pre-implementation run
# of this file a genuine RED (pytest collects fine and each test FAILS with
# rc==1) instead of a collection ERROR (rc==2) — the red_gate machine seat
# only accepts rc==1 as "red demonstrated" (task cb1dc6f4 red_gate:
# "red not demonstrated ... rc=2, wanted rc==1 test failures").

PINNED = "services/prism-service/tests/unit/test_pinned_by_delivery.py"
PASSING_TEST = "def test_x():\n    assert True\n"
FAILING_TEST = "def test_x():\n    assert False\n"


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, check=True)
    return r.stdout.strip()


def _init_main(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "services/prism-service/tests/unit").mkdir(parents=True)
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _worktree(repo: Path, ws_path: Path, branch: str) -> None:
    _git(repo, "worktree", "add", "-q", "-b", branch, str(ws_path), "main")


def _commit_pinned_test(ws: Path, task_id: str, body: str, subject: str) -> str:
    (ws / PINNED).parent.mkdir(parents=True, exist_ok=True)
    (ws / PINNED).write_text(body)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", f"{subject} [task:{task_id[:8]}]")
    return _git(ws, "rev-parse", "HEAD")


def _task(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=task_id, verify=[PINNED])


def _svc(task) -> tuple[ConductorService, "_FakeTaskSvc"]:
    svc = ConductorService.__new__(ConductorService)
    fake = _FakeTaskSvc(task)
    svc._task_svc = fake
    return svc, fake


class _FakeTaskSvc:
    def __init__(self, task):
        self._task = task
        self.history: list[tuple[str, dict]] = []

    def get(self, _tid):
        return self._task

    def record_history(self, tid, **kw):
        self.history.append((tid, kw))


# ---------------------------------------------------------------------
# PART 1 — branch resolution (prerequisite)
# ---------------------------------------------------------------------


def test_resolve_delivery_branch_prefers_the_tasks_own_workspace_branch(
    tmp_path, monkeypatch,
):
    """Reproduces the live bug: the checkout sits on an unrelated branch
    and the task's real commits live on its OWN prism/ws/<id> branch."""
    task_id = "a4c1bf03"
    repo = _init_main(tmp_path)
    branch = f"prism/ws/{task_id}"
    ws = tmp_path / "ws"
    _worktree(repo, ws, branch)
    tip = _commit_pinned_test(ws, task_id, PASSING_TEST, "the task's real work")

    # The shared checkout is on an UNRELATED branch, mirroring the live repro.
    _git(repo, "checkout", "-qb", "fix/some-other-thing")

    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"branch": branch, "path": str(ws),
                                    "repo_root": str(repo)} if tid == task_id
                        else None)

    from prism_service.api.tasks import _resolve_delivery_branch
    got_branch, rev = _resolve_delivery_branch(str(repo), task_id)
    assert got_branch == branch, (
        "must resolve the task's OWN branch, not the checkout's current one "
        f"(fix/some-other-thing); got {got_branch!r}")
    assert rev == branch
    log = _git(repo, "log", "--grep", f"task:{task_id[:8]}",
               "--format=%H", rev)
    assert tip in log.splitlines(), "the task's tagged commit must be found"


def test_resolve_delivery_branch_falls_back_when_no_workspace(
    tmp_path, monkeypatch,
):
    """No task_workspace record (never entered the flow, or already torn
    down after landing) -> old behavior: the checkout's current branch."""
    repo = _init_main(tmp_path)
    _git(repo, "checkout", "-qb", "whatever-is-checked-out")
    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for", lambda tid: None)
    from prism_service.api.tasks import _resolve_delivery_branch
    branch, rev = _resolve_delivery_branch(str(repo), "no-such-task")
    assert branch == "whatever-is-checked-out"
    assert rev == "HEAD"


def test_resolve_delivery_branch_ignores_a_torn_down_workspace_record(
    tmp_path, monkeypatch,
):
    """A stale index record naming a branch that no longer exists (worktree
    removed) must fall back cleanly, not error."""
    repo = _init_main(tmp_path)
    _git(repo, "checkout", "-qb", "main2")  # still resolvable; irrelevant here
    from prism_service.services import task_workspace
    monkeypatch.setattr(
        task_workspace, "workspace_for",
        lambda tid: {"branch": "prism/ws/gone", "path": "", "repo_root": ""})
    from prism_service.api.tasks import _resolve_delivery_branch
    branch, rev = _resolve_delivery_branch(str(repo), "gone-task")
    assert branch != "prism/ws/gone"


def test_get_task_delivery_endpoint_reports_the_tasks_own_branch(
    tmp_path, monkeypatch,
):
    """End-to-end through the real endpoint function: with the checkout on
    an unrelated branch, the Delivery card must still find the task's own
    tagged commit instead of reporting 'no commits found'."""
    task_id = "ae67ed5c"
    repo = _init_main(tmp_path)
    branch = f"prism/ws/{task_id}"
    ws = tmp_path / "ws2"
    _worktree(repo, ws, branch)
    _commit_pinned_test(ws, task_id, PASSING_TEST, "real work")
    _git(repo, "checkout", "-qb", "unrelated-checkout-branch")

    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"branch": branch, "path": str(ws),
                                    "repo_root": str(repo)} if tid == task_id
                        else None)

    import prism_service.api.tasks as tasks_mod
    from prism_service.services import claude_transcripts as ct

    monkeypatch.setattr(ct, "_project_source_path", lambda project: str(repo))
    fake_task_svc = SimpleNamespace(
        get=lambda tid: SimpleNamespace(gate_state="", workflow_step="",
                                        status="pending"))
    monkeypatch.setattr(tasks_mod, "get_project",
                        lambda project: SimpleNamespace(task_svc=fake_task_svc))

    result = get_task_delivery(task_id, project="prism")
    assert result["branch"] == branch
    assert result["commits"], (
        "the task's own tagged commit must be found, not 'no commits found' "
        f"(full result: {result})")


# ---------------------------------------------------------------------
# PART 2 — deliver_task: the guardrails ARE the feature
# ---------------------------------------------------------------------


def test_delivery_refuses_a_red_tree_and_leaves_main_untouched(
    tmp_path, monkeypatch,
):
    task_id = "redtreetask01"
    repo = _init_main(tmp_path)
    branch = f"prism/ws/{task_id}"
    ws = tmp_path / "ws"
    _worktree(repo, ws, branch)
    # commit A: pinned test PASSES (what an earlier receipt might have seen)
    _commit_pinned_test(ws, task_id, PASSING_TEST, "green commit")
    # commit B (the tree actually being merged): breaks the pinned test.
    tip = _commit_pinned_test(ws, task_id, FAILING_TEST, "broke it")

    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"branch": branch, "path": str(ws),
                                    "repo_root": str(repo)})

    before_main = _git(repo, "rev-parse", "refs/heads/main")
    svc, fake = _svc(_task(task_id))
    result = svc.deliver_task(task_id, actor="test-actor")

    assert result["state"] == "refused", result
    assert not result["ok"]
    assert tip[:12] in result["reason"]
    assert "RED" in result["reason"] or "red" in result["reason"].lower()

    after_main = _git(repo, "rev-parse", "refs/heads/main")
    assert after_main == before_main, "main must be byte-identical after a refusal"
    assert _git(repo, "status", "--porcelain") == ""
    assert not fake.history, "a refusal is not a delivery -- nothing to audit"


def test_delivery_reverifies_at_the_current_tip_not_a_stale_receipt(
    tmp_path, monkeypatch,
):
    """The exact misfire shape this session produced: a receipt minted at an
    OLDER tree (e58a6df) with a follow-up commit (767aa63) on top. deliver_task
    must never trust that older green state -- it re-runs the pinned tests
    at the CURRENT branch tip every time, so a later red commit is caught."""
    task_id = "stalerecpt01"
    repo = _init_main(tmp_path)
    branch = f"prism/ws/{task_id}"
    ws = tmp_path / "ws"
    _worktree(repo, ws, branch)
    older_green_tree = _commit_pinned_test(ws, task_id, PASSING_TEST,
                                           "e58a6df-shaped: green")
    newer_red_tip = _commit_pinned_test(ws, task_id, FAILING_TEST,
                                        "767aa63-shaped: follow-up breaks it")
    assert older_green_tree != newer_red_tip

    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"branch": branch, "path": str(ws),
                                    "repo_root": str(repo)})
    svc, _fake = _svc(_task(task_id))
    result = svc.deliver_task(task_id)
    assert result["state"] == "refused"
    assert result.get("verified_tree") == newer_red_tip, (
        "must have verified AT the current tip, not the older green commit")


def test_delivery_parks_on_conflict_and_leaves_main_untouched(
    tmp_path, monkeypatch,
):
    task_id = "conflicttask01"
    repo = _init_main(tmp_path)
    branch = f"prism/ws/{task_id}"
    ws = tmp_path / "ws"
    _worktree(repo, ws, branch)
    # Task branch edits README.md...
    (ws / "README.md").write_text("branch version\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", f"branch edit [task:{task_id[:8]}]")
    _commit_pinned_test(ws, task_id, PASSING_TEST, "add pinned test")
    tip = _git(ws, "rev-parse", "HEAD")

    # ...and so does main, on the same line, after the branch was cut.
    (repo / "README.md").write_text("main diverged\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main diverges on the same file")
    before_main = _git(repo, "rev-parse", "refs/heads/main")

    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"branch": branch, "path": str(ws),
                                    "repo_root": str(repo)})
    svc, fake = _svc(_task(task_id))
    result = svc.deliver_task(task_id)

    assert result["state"] == "parked", result
    assert not result["ok"]
    assert "conflict" in result["reason"].lower()
    after_main = _git(repo, "rev-parse", "refs/heads/main")
    assert after_main == before_main, "a conflict must leave main byte-identical"
    assert _git(repo, "status", "--porcelain") == "", (
        "merge-tree must never touch the working tree/index")
    assert not fake.history


def test_delivery_lands_a_clean_branch_on_main_and_audits_it(
    tmp_path, monkeypatch,
):
    task_id = "cleantask01"
    repo = _init_main(tmp_path)
    branch = f"prism/ws/{task_id}"
    ws = tmp_path / "ws"
    _worktree(repo, ws, branch)
    _commit_pinned_test(ws, task_id, PASSING_TEST, "add pinned test")
    (ws / "feature.txt").write_text("shipped\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", f"add the feature [task:{task_id[:8]}]")
    tip = _git(ws, "rev-parse", "HEAD")
    before_main = _git(repo, "rev-parse", "refs/heads/main")

    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"branch": branch, "path": str(ws),
                                    "repo_root": str(repo)})
    svc, fake = _svc(_task(task_id))
    result = svc.deliver_task(task_id, actor="conductor-adjudicator")

    assert result["state"] == "delivered", result
    assert result["ok"]
    assert result["verified_tree"] == tip
    new_main = result["main_sha"]
    assert new_main != before_main

    # PROVEN FROM GIT FACTS, not the call's own optimism.
    anc = subprocess.run(["git", "merge-base", "--is-ancestor", tip,
                         "refs/heads/main"], cwd=str(repo))
    assert anc.returncode == 0, "the delivered commits must be ancestors of main"
    log = _git(repo, "log", "--format=%s", "refs/heads/main")
    assert f"add the feature [task:{task_id[:8]}]" in log
    assert "add pinned test" in log

    # AUDITED on the task: approving actor, receipt-shaped detail, commits.
    assert fake.history, "the merge must be recorded on the task"
    _tid, kw = fake.history[-1]
    assert kw["action"] == "delivery"
    assert kw["actor"] == "conductor-adjudicator"
    assert tip[:12] in kw["details"]

    # Delivering the SAME tree again is a no-op, not a second merge commit.
    result2 = svc.deliver_task(task_id)
    assert result2["state"] == "already_delivered"
    assert _git(repo, "rev-parse", "refs/heads/main") == new_main


def test_delivery_is_unconditional_and_callable_without_the_env_switch(
    tmp_path, monkeypatch,
):
    """deliver_task() itself never consults PRISM_DELIVERY_AUTOMERGE -- only
    the AUTOMATIC trigger wired into gate_decide does. Direct callers
    (tests, tools, a future explicit 'ship this task' action) are unaffected
    by the switch being off."""
    monkeypatch.delenv("PRISM_DELIVERY_AUTOMERGE", raising=False)
    task_id = "directcall01"
    repo = _init_main(tmp_path)
    branch = f"prism/ws/{task_id}"
    ws = tmp_path / "ws"
    _worktree(repo, ws, branch)
    _commit_pinned_test(ws, task_id, PASSING_TEST, "work")

    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"branch": branch, "path": str(ws),
                                    "repo_root": str(repo)})
    svc, _fake = _svc(_task(task_id))
    result = svc.deliver_task(task_id)
    assert result["state"] == "delivered"


# ---------------------------------------------------------------------
# Wiring + the opt-in switch (mirrors test_green_gate_rejects_foreign_tree
# _receipt.py's own inspect.getsource style for pinning a call site without
# standing up the whole gate_decide machinery)
# ---------------------------------------------------------------------


def test_delivery_automerge_env_switch(monkeypatch):
    from prism_service.services.conductor_service import _delivery_enabled
    monkeypatch.delenv("PRISM_DELIVERY_AUTOMERGE", raising=False)
    assert _delivery_enabled() is False
    monkeypatch.setenv("PRISM_DELIVERY_AUTOMERGE", "1")
    assert _delivery_enabled() is True
    monkeypatch.setenv("PRISM_DELIVERY_AUTOMERGE", "0")
    assert _delivery_enabled() is False
    monkeypatch.setenv("PRISM_DELIVERY_AUTOMERGE", "true")
    assert _delivery_enabled() is True


def test_gate_decide_wires_delivery_behind_the_env_switch_at_green_gate():
    """Pin that a passed green_gate consults deliver_task ONLY inside the
    _delivery_enabled() guard -- so approving THIS task's own gate cannot
    merge a real branch into a real main unless an owner opted in."""
    import inspect
    src = inspect.getsource(ConductorService.gate_decide)
    assert "_delivery_enabled()" in src
    assert "self.deliver_task(" in src
    guard_idx = src.index("_delivery_enabled()")
    call_idx = src.index("self.deliver_task(")
    assert guard_idx < call_idx, (
        "the env-switch check must gate the call, not follow it")


# ---------------------------------------------------------------------
# Discovered while driving THIS task through the conductor's own red_gate
# (not part of the delivery feature itself, but a real defect this drive
# hit and fixed in the same allowed file): a red-tests commit that gets
# corrected via a soft-reset + recommit (e.g. fixing a collection-error
# shape so red is a genuine test FAILURE) leaves the OLD commit dangling
# but still resolvable in the shared git object store -- so it still LOOKS
# tests-only by content and used to shadow the new anchor in
# _red_step_sha forever, permanently blocking the adjudicator's red-gate
# retry (its `tried` cache keys on that same stale sha + spec_hash).
# ---------------------------------------------------------------------


class _HistorySvc:
    def __init__(self, rows):
        self._rows = rows

    def history(self, _tid):
        return self._rows


def test_red_step_sha_ignores_a_superseded_dangling_commit(tmp_path):
    repo = _init_main(tmp_path)
    (repo / "services/prism-service/tests/unit").mkdir(parents=True,
                                                        exist_ok=True)
    (repo / "services/prism-service/tests/unit/test_a.py").write_text(
        PASSING_TEST)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "old red commit [task:abc12345]")
    old_sha = _git(repo, "rev-parse", "HEAD")
    base = _git(repo, "rev-parse", "HEAD~1")

    # Rewrite the branch past `old_sha`: a soft-reset + recommit, exactly
    # what fixing a bad red-tests commit looks like. `old_sha` is now
    # unreachable from HEAD but its object still resolves.
    _git(repo, "reset", "--soft", base)
    (repo / "services/prism-service/tests/unit/test_a.py").write_text(
        FAILING_TEST)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "corrected red commit [task:abc12345]")
    new_sha = _git(repo, "rev-parse", "HEAD")
    assert old_sha != new_sha

    svc = ConductorService.__new__(ConductorService)
    svc._project_root = ""
    svc._task_svc = _HistorySvc(
        [{"action": "red_step_sha", "details": old_sha}])
    from prism_service.services import task_workspace
    import unittest.mock as mock
    with mock.patch.object(task_workspace, "workspace_for",
                          lambda tid: {"path": str(repo),
                                      "baseline": base, "branch": "main",
                                      "repo_root": str(repo)}):
        got = svc._red_step_sha("abc12345")
    assert got == new_sha, (
        f"must self-heal past the dangling superseded commit {old_sha[:12]} "
        f"to the real, current anchor {new_sha[:12]}; got {got[:12] if got else got}")
