"""A red green receipt sends the drive back to implement_tasks (task ad92c0e9).

Pins the plan's NEW non-policy module `services/green_rewind.py`
(`maybe_rewind(ctx, task, project)`) and the `conductor_flow._job` change
that surfaces the rewind reason in the implement_tasks instructions.
Trace: story AC-1..AC-8 (AC-9 is live evidence on the AOS dev instance).
Receipts are read through `oracle_spec.latest_receipt` and the tree through
`oracle_spec.current_tree_sha`; both are stubbed so no worktree is created.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

FAILING = [
    "tests/unit/test_x.py::test_alpha",
    "tests/unit/test_x.py::test_beta",
]
TREE = "abc123"


def _receipt(passed: bool, tree: str = TREE):
    from prism_service.services.oracle_spec import EvidenceReceipt
    obs = [{"name": n, "passed": False} for n in FAILING]
    obs.append({"name": "tests/unit/test_x.py::test_ok", "passed": True})
    return EvidenceReceipt(task_id="t", job_id="j", spec_hash="s",
                          tree_sha=tree, adapter="pytest_ids",
                          passed=passed,
                          status="passed" if passed else "failed",
                          observations=obs, reason="7 failed, 1 passed")


def _setup(tmp_path, monkeypatch, receipt, *, step="green_gate",
           gate_state="pending", budget=None):
    from prism_service.services import oracle_spec
    from prism_service.services.task_service import TaskService
    from prism_service.services import green_rewind  # NEW module (red)
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="rewind me",
                        verify=["services/prism-service/tests/unit/test_x.py"])
    task_svc.update(t.id, workflow_step=step, gate_state=gate_state)
    src = tmp_path / "src"
    (src / ".prism" / "behaviors").mkdir(parents=True)
    if budget is not None:
        (src / ".prism" / "behaviors" / "conductor.json").write_text(
            json.dumps({"rewind_budget": budget}), encoding="utf-8")
    monkeypatch.setattr(oracle_spec, "latest_receipt", lambda p, i: receipt)
    monkeypatch.setattr(oracle_spec, "current_tree_sha", lambda ws: TREE)
    monkeypatch.setattr(green_rewind, "_source_path",
                        lambda project: str(src), raising=False)
    ctx = types.SimpleNamespace(task_svc=task_svc, project="testproj")
    return green_rewind, task_svc, ctx, task_svc.get(t.id)


def _rewinds(task_svc, tid):
    return [h for h in task_svc.history(tid) if h.action == "rewind"]


def test_fresh_failed_receipt_rewinds(tmp_path, monkeypatch):
    """AC-1: fresh FAILED receipt at green_gate -> implement_tasks."""
    gr, svc, ctx, task = _setup(tmp_path, monkeypatch, _receipt(False))
    res = gr.maybe_rewind(ctx, task, "testproj")
    assert res and res["ok"] and res["to_step"] == "implement_tasks"
    t = svc.get(task.id)
    assert t.workflow_step == "implement_tasks"
    # SUPERSEDED 2026-08-30 by test_inconclusive_receipt_is_not_a_failure.py
    # (task 8fbd5cf0). This used to assert gate_state == "pending" after the
    # rewind. implement_tasks is an AGENT step and has no gate, so a pending
    # gate there is incoherent — and invisible to
    # task_service.is_open_gate_step(), which only fires when the step ITSELF
    # is a gate. That blind spot let task_runner's stall handler close a task
    # as done while its green_gate had never been decided. The real invariant
    # the old line was reaching for is that the rewind does not leave a
    # SETTLED gate behind, which "none" satisfies; the gate is recreated when
    # the flow advances back into green_gate.
    assert t.gate_state == "none"


def test_instructions_name_failing_tests(tmp_path, monkeypatch):
    """AC-2: the rewound implement_tasks job names each failing test id."""
    from prism_service.api import conductor_flow
    gr, svc, ctx, task = _setup(tmp_path, monkeypatch, _receipt(False))
    gr.maybe_rewind(ctx, task, "testproj")
    job = conductor_flow._job(svc.get(task.id))
    assert job["step"] == "implement_tasks"
    for name in FAILING:
        assert name in job["instructions"]


def test_stale_receipt_does_not_rewind(tmp_path, monkeypatch):
    """AC-3: a FAILED receipt at another tree does not rewind."""
    gr, svc, ctx, task = _setup(tmp_path, monkeypatch,
                                _receipt(False, tree="stale999"))
    assert gr.maybe_rewind(ctx, task, "testproj") is None
    assert svc.get(task.id).workflow_step == "green_gate"
    assert _rewinds(svc, task.id) == []


@pytest.mark.parametrize("receipt", [None, "passed"])
def test_passed_or_missing_receipt_does_not_rewind(tmp_path, monkeypatch,
                                                   receipt):
    """AC-4: a missing or PASSED receipt never rewinds."""
    rc = _receipt(True) if receipt == "passed" else None
    gr, svc, ctx, task = _setup(tmp_path, monkeypatch, rc)
    assert gr.maybe_rewind(ctx, task, "testproj") is None
    assert svc.get(task.id).workflow_step == "green_gate"
    assert _rewinds(svc, task.id) == []


def _park_again(svc, tid):
    svc.update(tid, workflow_step="green_gate", gate_state="pending")
    return svc.get(tid)


def test_third_red_receipt_parks_with_budget_reason(tmp_path, monkeypatch):
    """AC-5: after three rewinds the fourth red receipt parks."""
    gr, svc, ctx, task = _setup(tmp_path, monkeypatch, _receipt(False))
    for _ in range(3):
        assert gr.maybe_rewind(ctx, task, "testproj")["ok"] is True
        task = _park_again(svc, task.id)
    res = gr.maybe_rewind(ctx, task, "testproj")
    assert res["ok"] is False and res["parked"] is True
    t = svc.get(task.id)
    assert t.workflow_step == "green_gate" and t.gate_state == "pending"
    assert "Rewind budget 3 spent" in t.gate_reason
    for name in FAILING:
        assert name in t.gate_reason
    assert len(_rewinds(svc, task.id)) == 3


def test_budget_from_behaviour_file(tmp_path, monkeypatch):
    """AC-6: rewind_budget in .prism/behaviors/conductor.json; default 3."""
    gr, svc, ctx, task = _setup(tmp_path, monkeypatch, _receipt(False),
                                budget=1)
    assert gr.maybe_rewind(ctx, task, "testproj")["ok"] is True
    task = _park_again(svc, task.id)
    res = gr.maybe_rewind(ctx, task, "testproj")
    assert res["parked"] is True
    assert "Rewind budget 1 spent" in svc.get(task.id).gate_reason


@pytest.mark.parametrize("step,gate", [("green_gate", "passed"),
                                       ("red_gate", "pending")])
def test_never_rewinds_passed_gate_or_red_gate(tmp_path, monkeypatch,
                                               step, gate):
    """AC-7: a passed green_gate and any red_gate keep step and state."""
    gr, svc, ctx, task = _setup(tmp_path, monkeypatch, _receipt(False),
                                step=step, gate_state=gate)
    assert gr.maybe_rewind(ctx, task, "testproj") is None
    t = svc.get(task.id)
    assert t.workflow_step == step and t.gate_state == gate
    assert _rewinds(svc, task.id) == []


def test_rewind_writes_history_row(tmp_path, monkeypatch):
    """AC-8: each rewind writes ONE audited history row."""
    gr, svc, ctx, task = _setup(tmp_path, monkeypatch, _receipt(False))
    gr.maybe_rewind(ctx, task, "testproj")
    rows = _rewinds(svc, task.id)
    assert len(rows) == 1
    assert rows[0].actor == "conductor-adjudicator"
    assert "attempt=1" in rows[0].details
    for name in FAILING:
        assert name in rows[0].details
