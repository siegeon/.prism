"""A task blocked BY THE UNSHIPPED TOOTH is still shippable (task 5368c963).

On 2026-08-28 eleven finished, tested tasks sat permanently stranded at
`green_gate`. `_park_green_refusal` (conductor_service.py:2746) writes the
shipped-ness objection built at conductor_service.py:2913 into BOTH
`gate_reason` and `blocked_reason`; another seat (task_runner._handle_stall,
task_runner.py:359) then flips the row to `status="blocked"`. The machine
ship scan `_awaiting_ship_machine` (ship_worker.py:619) reads only
`task_svc.list(status="in_progress")`, so it never saw them: the gate cannot
pass until the branch lands, and the branch cannot land because the shipper
cannot see the task. The un-park self-heal is scoped to `status != "blocked"`
(conductor_service.py:2422), so nothing released them either.

The objection is BOOKKEEPING, not a defect in the work, so it must not
remove a task from the ship queue. The `likely_misfire` this suite guards:
admitting every blocked task would put genuinely broken work back in the
queue, where it fails again and starves healthy tasks. Admission must be a
POSITIVE match on the unshipped objection, never on `status == "blocked"`.

These tests drive the REAL `_awaiting_ship_machine` and the REAL
`sweep_once`, and leave `cond._unshipped_gate_reason` UNPATCHED against a
real bare `origin` + a genuinely unpushed `[task:<id8>]` commit — the
neighbouring suite test_ship_worker_machine_track.py:215-250 stubs that
tooth for its own eligibility tests; here it must run for real, because
AC-2's rebase-stalled task also has an unreachable trailer (live task
4e6e7417) and would pass that tooth too. Only the `blocked_reason` match
separates the two cases. `_walk_to_green_gate` from that suite is not
reused: it exercises gate history, which eligibility does not read.
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


def _git(cwd, *args) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _unshipped_workspace(tmp_path: Path, task_id: str):
    """Real bare `origin` + a work checkout whose `[task:<id8>]` commit sits
    on the task's own branch, unpushed — the fixture shape
    test_ship_worker_machine_track.py:53 establishes."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "README.md").write_text("# baseline\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "baseline")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "origin", "main")

    branch = f"prism/ws/{task_id}"
    _git(work, "checkout", "-q", "-b", branch)
    (work / "feature.txt").write_text("the change under test\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm",
         f"feat: the finished work of a task the tooth then blocked\n\n"
         f"[task:{task_id[:8]}]")
    return origin, work, branch


def _rebase_stall_reason(stage: str = "rebase") -> str:
    """The text `_note_ship_result` writes (ship_worker.py:596-602), built
    from that same f-string shape rather than hand-typed, so this negative
    test stays true if the wording moves."""
    from prism_service.services import ship_worker

    error = ("rebase onto origin/main conflicts in prism_service/services/"
             "ship_worker.py -- needs manual resolution")
    return (
        f"ship_worker: stuck at {stage} for {ship_worker.STALL_THRESHOLD} "
        f"consecutive identical attempts -- {error} (needs a manual fix -- "
        "e.g. resolving a merge conflict or fixing a broken CI check -- "
        "before ship_worker will retry it automatically)")


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def _wire_ws(monkeypatch, work: Path, branch: str, task_id: str):
    import prism_service.services.task_workspace as tw

    rec = {"task_id": task_id, "path": str(work), "branch": branch,
           "repo_root": str(work)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: dict(rec))
    monkeypatch.setattr(tw, "workspace_record", lambda tid: dict(rec),
                        raising=False)


def _parked_task(tmp_path, monkeypatch, *, status: str, blocked_reason,
                 proof_type: str = "test"):
    """A task at a pending green_gate whose `[task:<id8>]` commit is really
    unpushed, wired into the `default` project the scan reads.

    `blocked_reason=True` means "the real objection": the text
    `cond._unshipped_gate_reason` itself produces, written the way
    `_park_green_refusal` (conductor_service.py:2746) writes it. The tooth
    is never stubbed.
    """
    from prism_service.project_context import get_project

    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="finished work the tooth then blocked",
                        tags=["conductor"], proof_type=proof_type)
    _origin, work, branch = _unshipped_workspace(tmp_path, t.id)
    _wire_ws(monkeypatch, work, branch, t.id)

    objection = cond._unshipped_gate_reason(task_svc.get(t.id))
    assert objection, (
        "fixture must start GENUINELY unshipped — the real shipped-ness "
        "tooth has to raise the objection this suite is about")

    reason = objection if blocked_reason is True else blocked_reason
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending",
                    status=status, blocked_reason=reason, gate_reason=objection)

    ctx = get_project("default")
    monkeypatch.setattr(ctx, "_conductor_svc", cond, raising=False)
    monkeypatch.setattr(ctx, "_task_svc", task_svc, raising=False)
    return t.id, task_svc, cond, work, branch


# --- AC-1 ------------------------------------------------------------------

def test_blocked_by_unshipped_trailer_is_eligible(tmp_path, monkeypatch):
    """AC-1: a blocked task whose block IS the unshipped-trailer objection
    stays in the machine ship queue. This is the deadlock: the gate cannot
    pass until the branch lands, and the branch cannot land while the
    shipper cannot see the row."""
    from prism_service.services import ship_worker

    tid, task_svc, _cond, _work, _branch = _parked_task(
        tmp_path, monkeypatch, status="blocked", blocked_reason=True)

    snap = task_svc.get(tid)
    assert snap.status == "blocked"
    assert "commit trailer is not yet reachable from origin/main" in \
        (snap.blocked_reason or ""), repr(snap.blocked_reason)

    assert ship_worker._awaiting_ship_machine("default") == [tid], (
        "a task blocked by the bookkeeping objection alone must stay "
        "shippable — nothing else ever lands its branch")


# --- AC-2 ------------------------------------------------------------------

def test_blocked_by_rebase_stall_is_not_eligible(tmp_path, monkeypatch):
    """AC-2 / stop_if: a task blocked by a rebase conflict must NOT enter
    the ship queue. It sits at the same step, gate state and proof type,
    and its trailer is ALSO unreachable (live task 4e6e7417), so
    `_unshipped_gate_reason` does not separate it — only the
    `blocked_reason` match does."""
    from prism_service.services import ship_worker

    tid, task_svc, cond, _work, _branch = _parked_task(
        tmp_path, monkeypatch, status="blocked",
        blocked_reason=_rebase_stall_reason())

    snap = task_svc.get(tid)
    assert cond._unshipped_gate_reason(snap), (
        "the rebase-stalled task must ALSO look unshipped, or this test "
        "would prove nothing about which filter did the excluding")

    assert ship_worker._awaiting_ship_machine("default") == [], (
        "genuinely broken work must stay OUT of the queue — it fails "
        "repeatedly there and starves healthy tasks")


# --- AC-3 ------------------------------------------------------------------

@pytest.mark.parametrize("reason", [
    "",
    "waiting on the owner to answer a scope question",
])
def test_blocked_without_unshipped_reason_is_not_eligible(
        tmp_path, monkeypatch, reason):
    """AC-3: admission is a POSITIVE match on the objection. A blocked task
    with an empty or unrelated `blocked_reason` stays out, even though every
    other tooth passes."""
    from prism_service.services import ship_worker

    tid, _task_svc, _cond, _work, _branch = _parked_task(
        tmp_path, monkeypatch, status="blocked", blocked_reason=reason)

    assert ship_worker._awaiting_ship_machine("default") == [], (
        f"`status == 'blocked'` alone must never admit a task "
        f"(blocked_reason={reason!r})")
    assert tid  # the row exists; it is the filter that excluded it


# --- AC-4 ------------------------------------------------------------------

def test_in_progress_eligibility_is_unchanged(tmp_path, monkeypatch):
    """AC-4: the in_progress branch of the scan keeps its behaviour — an
    in_progress task at a pending green_gate with an unreachable trailer is
    still returned, and demo/review is still excluded (owner rule
    eaafdf75, ship_worker.py:639)."""
    from prism_service.services import ship_worker

    tid, _task_svc, _cond, _work, _branch = _parked_task(
        tmp_path, monkeypatch, status="in_progress", blocked_reason="")
    assert ship_worker._awaiting_ship_machine("default") == [tid]

    demo_id, _svc2, _c2, _w2, _b2 = _parked_task(
        tmp_path / "demo", monkeypatch, status="in_progress",
        blocked_reason="", proof_type="demo")
    assert ship_worker._awaiting_ship_machine("default") == [], (
        f"demo/review stays on the human track: {demo_id[:8]} leaked into "
        f"the machine queue")


# --- AC-5 ------------------------------------------------------------------

def test_sweep_once_attempts_the_blocked_task(tmp_path, monkeypatch):
    """AC-5: the widened eligibility reaches the real pipeline. Drives the
    REAL `sweep_once` (ship_worker.py:648) and records the ids it hands to
    `ship_task` — only the network/`gh` edge is replaced."""
    from prism_service.services import ship_worker

    tid, _task_svc, _cond, _work, _branch = _parked_task(
        tmp_path, monkeypatch, status="blocked", blocked_reason=True)

    seen: list[tuple] = []

    def _recorder(task_id, project="default", **kw):
        seen.append((task_id, project, kw.get("on_landed")))
        return {"ok": True, "stage": "merge", "error": "", "pr": "pr-1"}

    monkeypatch.setattr(ship_worker, "ship_task", _recorder)
    monkeypatch.setattr("prism_service.project_context.get_all_projects",
                        lambda: ["default"])

    res = ship_worker.sweep_once()

    assert [s[0] for s in seen] == [tid], (
        f"sweep_once must attempt the blocked task; it attempted {seen!r}")
    assert seen[0][2] is ship_worker._adjudicate_after_ship, (
        "it must land on the MACHINE track, so the adjudicator seat "
        "re-decides once shipped-ness is cleared")
    assert res is not None and res.get("ok") is True, res
