"""A FAILED post-land oracle re-check must never rewind an already-shipped
task back into implement_tasks (task d0b392b3).

Live regression: d0b392b3's own branch was merged to origin/main
(f39024cc), then ship_worker's `_reap_after_land` removed the task's
worktree. `gate_adjudicator`'s periodic sweep does not know the task has
already landed -- it re-ran `adjudicate_green_gate`, which re-minted the
oracle against the now-gone workspace (falling back to some other cwd),
collected zero tests (pytest rc=4, "no tests ran"), recorded that as a
FAILED EvidenceReceipt, and `_evaluate_green_gate_rewind` read "FAILED" in
the refusal prose and rewound the task to implement_tasks -- reopening
"implementation" of code that was already merged.

Fix: `_evaluate_green_gate_rewind` now checks shipped-ness FIRST (task's
own `[task:<id8>]` trailer reachable from origin/main), using a repo path
that still resolves after the task's own worktree has been reaped
(fallback: PRISM_SOURCE_PATH / cwd). A shipped task is parked at
green_gate with the refusal recorded, never bounced backward.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services.conductor_service import (ADJUDICATOR_SEAT,
                                                       ConductorService)

RED = "green_gate: oracle not evidenced — latest receipt FAILED: pytest_ids: tests/unit/test_demo_task_red_step.py -> rc=4 (1 warning in 0.00s). The token proof scorer is advisory only."


class FakeTaskService:
    def __init__(self, task):
        self.task, self.rows = task, []

    def get(self, task_id):
        return self.task

    def update(self, task_id, **fields):
        for k, v in fields.items():
            setattr(self.task, k, v)
        return self.task

    def record_history(self, task_id, action, **kw):
        self.rows.append(SimpleNamespace(action=action, actor=kw.get("actor"),
                                         model=kw.get("model"),
                                         details=kw.get("details", ""),
                                         reason=kw.get("reason", "")))

    def history(self, task_id):
        return list(self.rows)


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, check=True)
    return r.stdout.strip()


def _shipped_repo(tmp_path: Path, task_id: str) -> Path:
    """A real repo whose origin/main carries this task's [task:<id8>]
    trailer -- the exact `_is_shipped_on_main` signal, built with real git
    since that check shells out."""
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
    (repo / "feature.txt").write_text("work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"ship: {task_id[:8]} landed [task:{task_id[:8]}]")
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _unshipped_repo(tmp_path: Path) -> Path:
    """A real repo with an origin/main that carries NO trailer for this
    task -- the negative control."""
    repo = tmp_path / "repo_unshipped"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    bare = tmp_path / "origin_unshipped.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _svc(workspace_path):
    task = SimpleNamespace(id="d0b392b3-2548-4e91-858a-f0825ef4590e",
                           workflow_step="green_gate", gate_state="pending",
                           gate_reason="", blocked_reason="",
                           verify=["services/prism-service/tests/unit/"
                                   "test_demo_task_red_step.py"])
    svc = ConductorService.__new__(ConductorService)
    svc._task_svc = FakeTaskService(task)
    svc._project_name = "default"
    return svc, task, svc._task_svc


def _rewinds(fake):
    return [r for r in fake.rows if r.action == "auto_rewind"]


def test_failed_post_land_receipt_never_rewinds_a_shipped_task(
    tmp_path, monkeypatch,
):
    from prism_service.services import task_workspace
    repo = _shipped_repo(tmp_path, "d0b392b3-2548-4e91-858a-f0825ef4590e")
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    svc, task, fake = _svc(repo)

    res = svc._evaluate_green_gate_rewind(task, "sometree", True, RED, True)

    assert res is None
    assert task.workflow_step == "green_gate", (
        "a shipped task's own merged code must never be reopened for "
        f"implementation, but workflow_step is {task.workflow_step!r}")
    assert task.gate_state == "pending"
    assert _rewinds(fake) == [], (
        "no auto_rewind row may be written for an already-shipped task")
    assert "origin/main" in task.gate_reason or "already" in \
        task.gate_reason.lower()


def test_failed_post_land_receipt_survives_a_reaped_workspace(
    tmp_path, monkeypatch,
):
    """The live failure mode: ship_worker's `_reap_after_land` has already
    removed the task's own worktree by the time the adjudicator sweeps
    again, so `task_workspace.workspace_for` returns nothing. The shipped
    check must still find origin/main via the PRISM_SOURCE_PATH fallback,
    not silently skip the guard because the task's own checkout is gone."""
    from prism_service.services import task_workspace
    repo = _shipped_repo(tmp_path, "d0b392b3-2548-4e91-858a-f0825ef4590e")
    monkeypatch.setattr(task_workspace, "workspace_for", lambda _tid: None)
    monkeypatch.setenv("PRISM_SOURCE_PATH", str(repo))
    svc, task, fake = _svc(repo)

    res = svc._evaluate_green_gate_rewind(task, "", True, RED, True)

    assert res is None
    assert task.workflow_step == "green_gate"
    assert _rewinds(fake) == []


def test_failed_receipt_still_rewinds_a_genuinely_unshipped_task(
    tmp_path, monkeypatch,
):
    """Regression guard: the new shipped-ness check must not swallow the
    real backward edge for a task that has NOT landed yet."""
    from prism_service.services import task_workspace
    repo = _unshipped_repo(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    svc, task, fake = _svc(repo)

    res = svc._evaluate_green_gate_rewind(task, "sometree", True, RED, False)

    assert task.workflow_step == "implement_tasks"
    assert len(_rewinds(fake)) == 1
