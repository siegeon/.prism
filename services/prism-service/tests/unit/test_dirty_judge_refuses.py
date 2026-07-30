"""A dirty judge refuses to adjudicate — task 69233ca0.

THE INCIDENT: a lane wrote uncommitted edits directly into the DAEMON'S OWN
checkout (the tree the running process source-runs from) instead of its own
task worktree, including a control_plane.POLICY_FILES entry
(conductor_service.py). Nothing flagged it: control_plane already pins gate
policy CONTENT to a baseline ref (candidate_policy_edits / policy_hash), but
that only defends the DECISION path, never the RUNTIME — the daemon executes
whatever Python was on disk when it started. The edits stayed inert only
because the daemon kept its startup image; had it been bounced, its own gate
would have been decided by code the incident lane had just edited.

  AC-1  a POLICY_FILES entry dirty (uncommitted) in the daemon's own checkout
        -> control_plane.dirty_judge_reason() names it, and
        candidate_controls_judge_reason(task) — the tooth every existing
        conductor_service.py adjudication seat already calls — returns it too.
  AC-2  daemon checkout clean -> "" ; falls through to today's existing
        candidate-worktree-diff behavior unchanged (not a blanket stop).
  AC-3  an unrelated dirty file (not in POLICY_FILES) does not trip the guard.
  AC-4  the detector reads the DAEMON'S OWN checkout (_prism_repo_root),
        never a task worktree passed via ctx/ws.
  AC-5  GET /api/conductor/gate/readiness names the dirty policy file.
  AC-6  GET /api/conductor/gate/readiness is unaffected when the daemon
        checkout is clean.

Every fixture below is a DISPOSABLE tmp_path git repo standing in for "the
daemon's own checkout" — never E:\\.prism itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import control_plane as cp  # noqa: E402

_POLICY_REL = cp.POLICY_FILES[4]   # .../conductor_service.py — the incident file
_NONPOLICY_REL = "README.md"


def _git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _daemon_repo(tmp: Path) -> Path:
    """A disposable repo carrying every POLICY_FILES entry (so is_policy_file
    matches) plus a non-policy file, committed clean — stands in for "the
    daemon's own checkout", never E:\\.prism."""
    repo = tmp / "daemon"
    repo.mkdir()
    _git(repo, "init", "-q")
    for pf in cp.POLICY_FILES:
        _write(repo, pf, f"# {pf}\nBASELINE = 1\n")
    _write(repo, _NONPOLICY_REL, "# readme\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


# ---------------------------------------------------------------------------
# AC-1 / AC-2 / AC-3 — dirty_judge_reason / candidate_controls_judge_reason
# ---------------------------------------------------------------------------


def test_dirty_policy_file_in_daemon_checkout_is_named_and_refuses(
        tmp_path, monkeypatch):
    repo = _daemon_repo(tmp_path)
    monkeypatch.setattr(cp, "_prism_repo_root", lambda: repo)
    _write(repo, _POLICY_REL, "# edited uncommitted\nBASELINE = 2\n")

    dirty = cp.dirty_policy_files_in_daemon_checkout()
    assert dirty == [cp._norm(_POLICY_REL)], dirty

    reason = cp.dirty_judge_reason()
    assert reason and "conductor_service.py" in reason

    # the tooth every existing conductor_service.py adjudication seat
    # already calls picks up the SAME reason, for any task whatsoever —
    # this is what wires it into every seat with zero edits there.
    task = types.SimpleNamespace(id="any-task", tags=[])
    assert cp.candidate_controls_judge_reason(task) == reason


def test_clean_daemon_checkout_is_untouched_same_as_today(
        tmp_path, monkeypatch):
    repo = _daemon_repo(tmp_path)
    monkeypatch.setattr(cp, "_prism_repo_root", lambda: repo)

    assert cp.dirty_policy_files_in_daemon_checkout() == []
    assert cp.dirty_judge_reason() == ""

    # falls through to today's existing candidate-worktree-diff check,
    # which finds no worktree at all for this task id -> still "" — the
    # guard is not a blanket stop.
    task = types.SimpleNamespace(id="untracked-task-id", tags=[])
    assert cp.candidate_controls_judge_reason(task) == ""


def test_unrelated_dirty_file_does_not_trip_guard(tmp_path, monkeypatch):
    repo = _daemon_repo(tmp_path)
    monkeypatch.setattr(cp, "_prism_repo_root", lambda: repo)
    _write(repo, _NONPOLICY_REL, "# edited uncommitted readme\n")

    assert cp.dirty_policy_files_in_daemon_checkout() == []
    assert cp.dirty_judge_reason() == ""
    # not just quiet: an ordinary task's judge-reason is still clean too.
    task = types.SimpleNamespace(id="ordinary-task", tags=[])
    assert cp.candidate_controls_judge_reason(task) == ""


# ---------------------------------------------------------------------------
# AC-4 — daemon checkout, never the task worktree
# ---------------------------------------------------------------------------


def test_reads_daemon_checkout_not_task_worktree(tmp_path, monkeypatch):
    daemon = _daemon_repo(tmp_path)
    _write(daemon, _POLICY_REL, "# daemon dirty, uncommitted\nX = 9\n")
    monkeypatch.setattr(cp, "_prism_repo_root", lambda: daemon)

    # a SEPARATE, entirely clean task-worktree repo with no relation to the
    # daemon repo's dirt — if the detector wrongly consulted THIS instead of
    # the daemon checkout it would read clean and wrongly return "".
    task_repo = tmp_path / "task_wt"
    task_repo.mkdir()
    _git(task_repo, "init", "-q")
    _write(task_repo, "some/file.py", "X = 1\n")
    _git(task_repo, "add", "-A")
    _git(task_repo, "commit", "-qm", "baseline")
    baseline = _git(task_repo, "rev-parse", "HEAD")
    monkeypatch.setattr(
        cp, "_workspace_for",
        lambda task_id: {"path": str(task_repo), "baseline": baseline,
                         "repo_root": str(task_repo)})

    task = types.SimpleNamespace(id="some-task-id", tags=[])
    reason = cp.candidate_controls_judge_reason(task)
    assert reason, "must refuse — the daemon checkout is dirty on policy"
    assert "conductor_service.py" in reason


# ---------------------------------------------------------------------------
# AC-5 / AC-6 — GET /api/conductor/gate/readiness
# ---------------------------------------------------------------------------


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def _green_gate_task(task_svc):
    t = task_svc.create(title="some green-gate task", tags=["conductor"],
                        oracle="pytest tests/unit/test_x.py -q",
                        proof_type="test",
                        likely_misfire="looks fine but is not")
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending")
    return t


def test_gate_readiness_names_dirty_policy_file(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    daemon = _daemon_repo(tmp_path)
    _write(daemon, _POLICY_REL, "# dirty, uncommitted\nX = 3\n")
    monkeypatch.setattr(cp, "_prism_repo_root", lambda: daemon)

    task_svc, cond = _services(tmp_path)
    t = _green_gate_task(task_svc)
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=t.id, project="default")
    assert out["receipt_ok"] is False
    assert "conductor_service.py" in out["receipt_refusal"]


def test_gate_readiness_untouched_when_daemon_checkout_clean(
        tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    daemon = _daemon_repo(tmp_path)   # clean — no edits after baseline commit
    monkeypatch.setattr(cp, "_prism_repo_root", lambda: daemon)

    task_svc, cond = _services(tmp_path)
    t = _green_gate_task(task_svc)
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=t.id, project="default")
    # unaffected by the new guard: the standard oracle-receipt tooth still
    # decides (refusing here for the ordinary "no receipt on file" reason,
    # not the dirty-judge reason) — proves the clean case is untouched.
    assert out["receipt_ok"] is False
    assert "conductor_service.py" not in out["receipt_refusal"]
    assert "daemon's own checkout" not in out["receipt_refusal"]
