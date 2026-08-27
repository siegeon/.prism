"""The policy judge stops blaming merged commits [task:dd2b87c8].

control_plane.candidate_policy_edits diffs the task worktree against the FROZEN
ws["baseline"] snapshot, so once the branch merges origin/main forward every
foreign policy-file commit is attributed to the candidate and the machine seat
abstains on an innocent slice (4d399e0a 2026-08-04, d1854966 2026-08-17). The
fix diffs against `git merge-base origin/main HEAD` re-evaluated at judge time.
Second half: the abstention reason must be RECORDED at the seat (pending +
gate_reason + one audit row, de-duped per sweep), never discarded.

AC-1/2/4 drive REAL git history (upstream repo + clone with origin/main), no
mocking of the judge itself. AC-3/5 pin the seat-side park helper against a
minimal fake TaskService, plus a source read asserting no seat still does the
bare `return None` that threw the reason away.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
from pathlib import Path
import pytest
_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
from prism_service.services import control_plane as _cp  # noqa: E402
_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
_TASK_ID = "dd2b87c8-test-policy-judge"


def _run(cwd: Path, *args: str) -> str:
    e = {**os.environ, **_GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, check=True, env=e,
                          capture_output=True, text=True).stdout.strip()


def _policy_rel() -> str:
    files = sorted(_cp.POLICY_FILES)
    assert files, "POLICY_FILES must not be empty"
    return files[0]


def _commit(repo: Path, rel: str, text: str, msg: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", msg)
    return _run(repo, "rev-parse", "HEAD")


class _Task:
    def __init__(self, tid: str = _TASK_ID):
        self.id = tid
        self.tags: list[str] = []
        self.gate_state = "pending"
        self.gate_reason = ""
        self.blocked_reason = ""
        self.workflow_step = "red_gate"


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """upstream (origin, branch main @ c1) + clone `wt` on branch task.
    The workspace index is redirected so the judge sees `wt` with the FROZEN
    baseline c1 -- the exact bookkeeping that produced the false positive."""
    monkeypatch.delenv("PRISM_POLICY_CHANGE_APPROVED", raising=False)
    up = tmp_path / "upstream"
    up.mkdir()
    _run(up, "init", "-q", "-b", "main")
    c1 = _commit(up, "README.md", "one", "c1")
    wt = tmp_path / "wt"
    _run(tmp_path, "clone", "-q", str(up), str(wt))
    _run(wt, "checkout", "-qb", "task")
    ws = {"path": str(wt), "baseline": c1, "branch": "task"}
    monkeypatch.setattr(_cp, "_workspace_for",
                        lambda tid: ws if tid == _TASK_ID else None)
    return up, wt, c1


def _merge_main_forward(up: Path, wt: Path, rel: str, text: str, msg: str) -> str:
    """A FOREIGN commit lands on origin/main and the task branch merges it."""
    sha = _commit(up, rel, text, msg)
    _run(wt, "fetch", "-q", "origin")
    _run(wt, "merge", "-q", "--no-edit", "origin/main")
    return sha


def test_ac1_foreign_policy_commits_merged_forward_are_not_blamed(repos):
    up, wt, _c1 = repos
    _merge_main_forward(up, wt, _policy_rel(), "# foreign\n", "foreign policy")
    assert _cp.candidate_policy_edits(_TASK_ID) == []
    assert _cp.candidate_policy_edit_reason(_Task()) == ""


def test_ac2_own_policy_commit_still_refuses_with_same_reason(repos):
    up, wt, _c1 = repos
    _merge_main_forward(up, wt, "README.md", "two", "foreign plain")
    rel = _policy_rel()
    _commit(wt, rel, "# candidate\n", "candidate edits judge")
    assert _cp.candidate_policy_edits(_TASK_ID) == [rel]
    assert _cp.candidate_policy_edit_reason(_Task()) == (
        "candidate modified gate policy: " + rel
        + "; policy must change through the control-plane, not the task "
        "under test — route an authorized policy change via "
        "PRISM_POLICY_CHANGE_APPROVED=1 or a 'policy-change'-tagged task")


def test_ac4_diff_base_is_merge_base_and_no_laundering(repos, monkeypatch):
    up, wt, _c1 = repos
    rel = _policy_rel()
    _commit(wt, rel, "# candidate\n", "candidate edits judge")
    seen: list[str] = []
    from prism_service.services import verifier_service as _vs
    real = _vs._git_changed_files
    monkeypatch.setattr(_vs, "_git_changed_files",
                        lambda ws, baseline=None: (seen.append(baseline or ""),
                                                   real(ws, baseline))[1])
    for n in range(2):  # main advances + is merged forward; candidate never lands
        _merge_main_forward(up, wt, "README.md", f"adv{n}", f"advance {n}")
        mb = _run(wt, "merge-base", "origin/main", "HEAD")
        assert mb != _c1, "merge-base must have moved off the frozen baseline"
        assert _cp.candidate_policy_edits(_TASK_ID) == [rel]
        assert seen[-1] == mb, "judge must diff against merge-base at judge time"


class _FakeTaskSvc:
    def __init__(self, task):
        self.task, self.history = task, []

    def get(self, tid):
        return self.task

    def update(self, tid, **kw):
        for k, v in kw.items():
            setattr(self.task, k, v)
        return self.task

    def record_history(self, tid, action, details=""):
        self.history.append((action, details))


def _seat():
    from prism_service.services.conductor_service import ConductorService
    svc = ConductorService.__new__(ConductorService)
    task = _Task()
    svc._task_svc = _FakeTaskSvc(task)
    return svc, task


def test_ac3_ac5_seat_records_abstention_pending_once():
    svc, task = _seat()
    reason = "candidate modified gate policy: x; policy must change ..."
    for _ in range(3):  # three sweeps over the SAME unresolved condition
        svc._park_policy_abstention(task.id, "red_gate", reason)
    assert task.gate_state == "pending"
    assert task.gate_reason == reason and task.blocked_reason == reason
    rows = [d for a, d in svc._task_svc.history if a == "gate_decide"]
    assert len(rows) == 1 and "gate=red_gate" in rows[0] and reason in rows[0]


def test_ac3_every_seat_records_the_reason_instead_of_discarding_it():
    src = (_SERVICE_ROOT / "prism_service" / "services"
           / "conductor_service.py").read_text(encoding="utf-8")
    bare = re.findall(r"if _cp\.candidate_controls_judge_reason\(task\):\s*\n"
                      r"\s*return None", src)
    assert bare == [], f"{len(bare)} seat(s) still discard the reason"
    assert src.count("self._park_policy_abstention(") >= 3
