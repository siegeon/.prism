"""The ship bot: owner Approve lands the branch — task 5b6aefc1.

DEFECT this suite pins: a demo green_gate whose `[task:<id8>]` commits live
only on the task's own workspace branch REFUSES the owner's approve
(conductor_service.py `_unshipped_gate_reason`, consulted at the gate_decide
pre-flight), telling the owner to go land the branch by hand. Measured cost
on the live board: 73 recorded `machine=refused` rows on task f506ece4, 16 on
b835f639, every one carrying the same "not yet reachable from origin/main"
banner — and those counts UNDERSTATE the clicks, because the park helper only
writes a row when the reason CHANGES.

Owner directive 2026-08-18: "not when I approve it you ship it. Make sure that
approve SHIPS it using the bot workflows." REFINEMENT: the pipeline (push ->
PR -> CI wait -> merge) is DETERMINISTIC CODE, "not with tokens, unless there
is an error, then that".

  AC-2  the conductor-shipper seat lands the branch: push -> pr create (body
        carries the [task:id8] trailer) -> pr checks polled -> pr merge, IN
        THAT ORDER, after which the trailer is reachable from origin/main.
  AC-3  zero LLM tokens on the happy path: the whole pipeline completes with
        `claude` unavailable, and no argv the pipeline issues invokes it.
  AC-5  every stage failure parks with the VERBATIM stderr and PRESERVES the
        recorded approval so a later sweep retries. Never writes `failed`.
  AC-6  "conductor-shipper" is a REGISTERED machine seat (an unregistered
        actor writing history resolves UNKNOWN — the audited defect).
  AC-7  default OFF: with PRISM_SHIP_ON_APPROVE unset the worker allocates no
        thread, mirroring start_task_runner/start_gate_adjudicator.

The gh boundary is the ONLY thing stubbed: git runs for real against a real
bare origin, so "shipped" is measured by the same helper the Dashboard uses
(`api/tasks.py::_is_shipped_on_main`), never by asserting on our own mock.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

TASK_ID = "5b6aefc1-fe98-48ee-b029-63ca8d572ee6"


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
    """A real bare `origin` + a work checkout whose `[task:<id8>]` commit sits
    on the task's own branch and has NOT been pushed. This is the exact live
    shape the unshipped tooth refuses on."""
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
         f"feat: ship bot lands the branch on approve\n\n[task:{task_id[:8]}]")
    return origin, work, branch


class FakeGh:
    """Records every argv; runs `git` FOR REAL and answers only `gh`.

    `gh pr merge` performs the real merge into origin's main, so shipped-ness
    is afterwards measured by real git — the test can never pass by agreeing
    with its own stub.
    """

    def __init__(self, origin: Path, branch: str, *, fail_at: str = "",
                 fail_err: str = "", checks_rc: int = 0):
        self.origin = origin
        self.branch = branch
        self.calls: list[list] = []
        self.fail_at = fail_at
        self.fail_err = fail_err
        self.checks_rc = checks_rc

    def __call__(self, argv, cwd=None):
        self.calls.append(list(argv))
        head = " ".join(str(a) for a in argv[:3])

        if argv[0] == "git":
            if self.fail_at == "push" and argv[1:2] == ["push"]:
                return 1, "", self.fail_err
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
            if self.fail_at == "pr_create":
                return 1, "", self.fail_err
            return 0, "https://github.com/siegeon/.prism/pull/42\n", ""
        if "pr checks" in head:
            if self.fail_at == "ci_wait":
                return self.checks_rc or 1, "", self.fail_err
            return 0, "all checks passed\n", ""
        if "pr merge" in head:
            if self.fail_at == "merge":
                return 1, "", self.fail_err
            # a REAL squash-style landing: push the branch onto origin/main
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

    def stages(self) -> list[str]:
        """The pipeline's observable stage order, from the argv stream."""
        out = []
        for c in self.calls:
            head = " ".join(str(a) for a in c[:3])
            if c[0] == "git" and c[1:2] == ["push"]:
                out.append("push")
            elif "pr create" in head:
                out.append("pr_create")
            elif "pr checks" in head:
                out.append("ci_wait")
            elif "pr merge" in head:
                out.append("merge")
        return out


def _shipped(repo: Path, task_id: str = TASK_ID) -> bool:
    from prism_service.api.tasks import _is_shipped_on_main
    _git(repo, "fetch", "-q", "origin")
    return _is_shipped_on_main(str(repo), task_id)


def _wire_ws(monkeypatch, work: Path, branch: str, task_id: str = TASK_ID):
    import prism_service.services.task_workspace as tw
    rec = {"task_id": task_id, "path": str(work), "branch": branch,
           "repo_root": str(work)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: dict(rec))
    monkeypatch.setattr(tw, "workspace_record", lambda tid: dict(rec),
                        raising=False)


# ---------------------------------------------------------------------------
# AC-2 — the pipeline lands the branch, in order, measured by real git
# ---------------------------------------------------------------------------


def test_pipeline_pushes_opens_pr_waits_for_ci_then_merges(
        tmp_path, monkeypatch):
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)
    assert not _shipped(work), "fixture must start UNshipped"

    gh = FakeGh(origin, branch)
    res = ship_worker.ship_task(TASK_ID, runner=gh, poll_interval_s=0)

    assert res["ok"] is True, res
    assert gh.stages() == ["push", "pr_create", "ci_wait", "merge"], gh.calls

    body = " ".join(
        " ".join(str(a) for a in c) for c in gh.calls
        if "pr create" in " ".join(str(a) for a in c[:3]))
    assert f"[task:{TASK_ID[:8]}]" in body, (
        "the PR must carry the [task:id8] trailer — it is what "
        "_is_shipped_on_main greps for on origin/main")

    assert _shipped(work), (
        "after the pipeline the trailer must be reachable from origin/main, "
        "measured by the Dashboard's own helper")


# ---------------------------------------------------------------------------
# AC-3 — deterministic code, zero LLM tokens
# ---------------------------------------------------------------------------


def test_pipeline_completes_with_claude_unavailable(tmp_path, monkeypatch):
    """The owner's refinement: the merge is handled programmatically, "not
    with tokens".

    Plant a BOOBY-TRAPPED `claude` first on PATH — invoking it writes a
    marker file. That is a stronger oracle than stripping PATH (which would
    only prove the pipeline dies without a shell), because it proves the
    happy path never reached for the model even when the model was there for
    the taking."""
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)

    bindir = tmp_path / "trap_bin"
    bindir.mkdir()
    marker = tmp_path / "claude_was_invoked"
    trap = bindir / "claude"
    trap.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n', encoding="utf-8")
    trap.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH','')}")

    gh = FakeGh(origin, branch)
    res = ship_worker.ship_task(TASK_ID, runner=gh, poll_interval_s=0)

    assert res["ok"] is True, res
    assert not marker.exists(), (
        "the happy path invoked `claude` — the ship pipeline must be "
        "deterministic code; the model enters only to diagnose a FAILURE")
    for call in gh.calls:
        assert "claude" not in str(call[0]).lower(), (
            f"the happy path spent LLM tokens: {call}")

    src = Path(ship_worker.__file__).read_text(encoding="utf-8")
    assert "claude_runner" not in src, (
        "the shipper must not import the claude runner — the LLM enters only "
        "on failure diagnosis, which is not this module's happy path")


# ---------------------------------------------------------------------------
# AC-5 — every stage failure parks with the VERBATIM error, approval preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage,err", [
    ("push", "! [rejected] prism/ws/5b6aefc1 -> non-fast-forward"),
    ("pr_create", "GraphQL: A pull request already exists for this branch"),
    ("ci_wait", "check 'build' failed: exit 1"),
    ("merge", "Merge conflict in services/conductor_service.py"),
])
def test_stage_failure_parks_with_verbatim_error(
        tmp_path, monkeypatch, stage, err):
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)

    gh = FakeGh(origin, branch, fail_at=stage, fail_err=err)
    res = ship_worker.ship_task(TASK_ID, runner=gh, poll_interval_s=0)

    assert res["ok"] is False, res
    assert res["stage"] == stage, res
    assert err in res["error"], (
        "the EXACT stage error must survive to the caller — a computed-then-"
        "dropped refusal is the e0149f1f defect class this repo already "
        f"named. got: {res.get('error')!r}")
    assert not _shipped(work), "a failed pipeline must not have landed anything"


def test_gh_missing_is_a_parked_reason_not_a_crash(tmp_path, monkeypatch):
    """The daemon environment may lack gh entirely. That is a REASON, not a
    stack trace (the plan's explicit requirement)."""
    from prism_service.services import ship_worker

    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)

    def _no_gh(argv, cwd=None):
        if argv[0] == "gh":
            raise FileNotFoundError("No such file or directory: 'gh'")
        return FakeGh(origin, branch)(argv, cwd)

    res = ship_worker.ship_task(TASK_ID, runner=_no_gh, poll_interval_s=0)
    assert res["ok"] is False, res
    assert "gh" in res["error"].lower(), res


def test_a_failed_ship_preserves_the_recorded_approval(tmp_path, monkeypatch):
    """R1: the human decision must NEVER be eaten by a later failure. The
    recorded approve row is both the queue and the receipt, so after a failed
    pipeline the task is still findable as ship-approved and a second sweep
    retries."""
    from prism_service.services import ship_worker
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService

    origin, work, branch = _unshipped_workspace(tmp_path)
    _wire_ws(monkeypatch, work, branch)

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    t = task_svc.create(title="ship me", tags=["conductor"])
    task_svc.record_history(
        t.id, action="gate_decide",
        details=("gate=green_gate; action=approve; human=recorded; "
                 "ship=queued; actor=siegeon@gmail.com; reason=looks right"),
        actor="siegeon@gmail.com")

    before = cond._recorded_ship_approval(t.id)
    assert before and before.get("actor") == "siegeon@gmail.com", before

    gh = FakeGh(origin, branch, fail_at="push", fail_err="! [rejected]")
    ship_worker.ship_task(t.id, runner=gh, poll_interval_s=0,
                          task_svc=task_svc, cond=cond)

    after = cond._recorded_ship_approval(t.id)
    assert after and after.get("actor") == "siegeon@gmail.com", (
        "a failed ship must leave the owner's recorded approval intact for "
        f"retry; got {after!r}")
    assert str(task_svc.get(t.id).gate_state or "") != "failed", (
        "a ship failure parks PENDING — writing `failed` would force the "
        "next honest approve through override=true (task 97d92854)")


# ---------------------------------------------------------------------------
# AC-6 — the seat is registered
# ---------------------------------------------------------------------------


def test_conductor_shipper_is_a_registered_machine_seat():
    from prism_service.models.actor import ActorKind
    from prism_service.services import actor_service
    from prism_service.services.ship_worker import SEAT_ID

    assert SEAT_ID == "conductor-shipper"
    assert SEAT_ID in actor_service.MACHINE_SEATS, (
        "an unregistered actor writing history resolves to UNKNOWN — the "
        "exact audited defect this task must not repeat")
    resolved = actor_service.ActorService().resolve(SEAT_ID)
    assert resolved.kind is ActorKind.MACHINE, resolved


# ---------------------------------------------------------------------------
# AC-7 — default OFF
# ---------------------------------------------------------------------------


def test_worker_is_off_by_default(monkeypatch):
    from prism_service.services import ship_worker

    monkeypatch.delenv("PRISM_SHIP_ON_APPROVE", raising=False)
    monkeypatch.delenv("PRISM_SHIP_ON_APPROVE_INTERVAL", raising=False)
    assert ship_worker.is_enabled() is False
    assert ship_worker.start_ship_worker() is None, (
        "mirrors start_task_runner/start_gate_adjudicator: no opt-in, no "
        "thread, no cost")


def test_env_opts_the_environment_in(monkeypatch):
    from prism_service.services import ship_worker

    monkeypatch.setenv("PRISM_SHIP_ON_APPROVE", "1")
    assert ship_worker.is_enabled() is True


def test_lifespan_wires_the_worker():
    """The seat is useless unwired — same guard the task_runner suite keeps."""
    import prism_service.main as m

    src = Path(m.__file__).read_text(encoding="utf-8")
    assert "start_ship_worker" in src, (
        "start_ship_worker() must be called from the real lifespan")
