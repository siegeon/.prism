"""A stalled task whose work already shipped is closed, never split.

Live waste, 2026-08-29: task 4cbac65a's fix landed on origin/main as
b2f8d88f. Because it shipped out of band, the runner kept driving its
verify_green_state step, stalled after three attempts, and split it into
SIX children -- "Make test_workspace_path_returns_the_indexed_directory
green" and five more -- every one of those tests already green on main.
Six pieces of work were manufactured that could only ever be waste, and
they sat in a queue that drives one step per tick.

_handle_stall must ask the question the gate already asks: is this task's
own [task:<id8>] trailer reachable from origin/main? If it is, the work is
done and the honest move is to close the task. If it is not, the split
stands exactly as before.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PROOF = ("still red: FAILED tests/unit/test_a.py::test_one and "
          "FAILED tests/unit/test_b.py::test_two")


class _Task:
    def __init__(self, tid):
        self.id = tid
        self.completion_proof = _PROOF
        self.priority = 50
        self.tags = ["x"]
        self.status = "in_progress"


class _Svc:
    """Records what _handle_stall did, without a database."""

    def __init__(self, tid):
        self.task = _Task(tid)
        self.created = []
        self.updates = []
        self.history_rows = []
        self.seeded_history = []

    def seed_green_gate_pass(self):
        self.seeded_history.append(type("H", (), {
            "action": "gate_decide",
            "details": "gate=green_gate; action=approve; verifier=pass",
        })())

    def get(self, tid):
        return self.task

    def history(self, tid):
        # A REAL green_gate approval can now be seeded (task 8fbd5cf0,
        # 2026-08-30): the shipped-close path requires one, because a trailer
        # on origin/main says the work exists, never that anybody judged it.
        return list(self.seeded_history)

    def create(self, **kw):
        self.created.append(kw)
        return type("C", (), {"id": f"child-{len(self.created)}"})()

    def update(self, tid, **kw):
        self.updates.append(kw)

    def record_history(self, tid, **kw):
        self.history_rows.append(kw)


def test_a_stalled_task_whose_trailer_is_on_main_is_closed_not_split(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda tid: True,
                        raising=False)
    svc = _Svc("4cbac65a-91cb-4ce3-95bc-bb5ec0979278")
    # SUPERSEDED 2026-08-30 by task 8fbd5cf0: closing on shipped-ness now
    # requires a real green_gate approval in the task's own history. Task
    # 8fbd5cf0 was falsely closed THREE times because "no OPEN gate" was
    # treated as "adjudicated" — an agent step has gate_state="none", so
    # shipped-ness alone closed a task whose driver was still landing
    # commits. The invariant THIS test protects — shipped work is never
    # split into waste children — is unchanged and still asserted below.
    svc.seed_green_gate_pass()
    out = tr._handle_stall(svc, svc.task.id, "verify_green_state")

    assert svc.created == [], (
        f"the stall split created {len(svc.created)} children for work that "
        "is already on origin/main")
    statuses = [u.get("status") for u in svc.updates if "status" in u]
    assert "blocked" not in statuses, (
        "shipped work must not be blocked; it is finished")
    assert "done" in statuses, (
        "a stalled task whose trailer is on origin/main must be closed")
    assert out["stalled"]["action"] == "shipped"


def test_an_unshipped_stalled_task_still_splits_exactly_as_before(monkeypatch):
    """The guard must be narrow. Unshipped work keeps the old behaviour."""
    from prism_service.services import task_runner as tr

    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda tid: False,
                        raising=False)
    svc = _Svc("11111111-2222-3333-4444-555555555555")
    out = tr._handle_stall(svc, svc.task.id, "implement_tasks")

    assert len(svc.created) == 2, svc.created
    assert out["stalled"]["action"] == "decomposed"
    assert [u.get("status") for u in svc.updates if "status" in u] == ["blocked"]


def test_the_shipped_check_exists_and_never_raises(monkeypatch):
    """A helper called inside a stall handler must fail closed: if git or
    the repo is unavailable it answers False, so the old split still runs
    rather than a task being closed on an error."""
    from prism_service.services import task_runner as tr

    fn = getattr(tr, "_stall_work_is_shipped", None)
    assert callable(fn), "no shipped-ness check in the stall path"

    def _boom(*a, **k):
        raise RuntimeError("git is gone")

    monkeypatch.setattr(tr, "_shipped_sha_for_stall", _boom, raising=False)
    assert fn("any-task-id") is False
