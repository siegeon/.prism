"""An oracle the runner COULD NOT JUDGE must not be read as a failure.

OBSERVED LIVE on task 8fbd5cf0 (2026-08-30). Its green_gate carried a
proof_type=demo oracle reading "Open /workflows and watch a task advance..."
— prose with no literal URL. The browser adapter answered:

    passed=False, status=manual_evidence_required,
    reason="browser: no loadable URL found in the oracle text"

That means NO AUTOMATED VERDICT IS AVAILABLE. It does not mean the work is
wrong. `green_rewind` tested only `receipt.passed`, which is False for every
non-pass — ST_MANUAL and ST_ERROR included — so it rewound green_gate off a
task whose work was already on origin/main.

Owner rule, 2026-08-30: "the gate is yours by destination. unless p95 unsure
of the result." Below the confidence bar the move is to ESCALATE with a named
reason, never to reject.

Second defect pinned here: a rewind lands on implement_tasks, an AGENT step,
and used to write gate_state="pending" there. A non-gate step carrying an open
gate is incoherent, and invisible to task_service.is_open_gate_step() — which
is exactly how the stall handler closed 8fbd5cf0 as done over an undecided
gate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass
class _Receipt:
    passed: bool
    status: str
    tree_sha: str


class _Ctx:
    def __init__(self, task_svc):
        self.task_svc = task_svc


def _svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / f"t-{uuid.uuid4().hex[:8]}.db"))


def _gated_task(svc):
    t = svc.create(title="a demo oracle the browser adapter cannot probe")
    svc.update(t.id, status="in_progress", workflow_step="green_gate",
               gate_state="pending")
    return svc.get(t.id)


def _arrange(monkeypatch, tmp_path, status):
    from prism_service.services import green_rewind as gr
    from prism_service.services import oracle_spec
    svc = _svc(tmp_path)
    task = _gated_task(svc)
    tree = "6189234b4f167c27feb57f304a65eb22d55de035"
    monkeypatch.setattr(oracle_spec, "latest_receipt",
                        lambda _p, _t: _Receipt(False, status, tree))
    monkeypatch.setattr(oracle_spec, "current_tree_sha", lambda _p: tree)
    monkeypatch.setattr(gr, "_workspace_path", lambda _t: "/tmp")
    return gr, svc, task


def test_a_manual_evidence_receipt_does_not_rewind(monkeypatch, tmp_path):
    from prism_service.services import oracle_spec
    gr, svc, task = _arrange(monkeypatch, tmp_path, oracle_spec.ST_MANUAL)

    res = gr.maybe_rewind(_Ctx(svc), task, "proj")

    after = svc.get(task.id)
    assert after.workflow_step == "green_gate", (
        "an inconclusive receipt means the runner could not judge; rewinding "
        f"the gate off it rejects work nobody assessed -- got {after.workflow_step!r}")
    assert res is not None and res.get("inconclusive") is True, res
    assert "not a failure" in str(res.get("reason", "")), (
        f"the reason must say plainly that this is not a failure; got {res!r}")


def test_a_genuinely_failed_receipt_still_rewinds(monkeypatch, tmp_path):
    """THE GUARD ON THE FIX: a real FAILED receipt must still rewind, or the
    auto-rewind loop this exists for is dead."""
    from prism_service.services import oracle_spec
    gr, svc, task = _arrange(monkeypatch, tmp_path, oracle_spec.ST_FAILED)

    res = gr.maybe_rewind(_Ctx(svc), task, "proj")

    assert res is not None and res.get("ok") is True, res
    assert svc.get(task.id).workflow_step == "implement_tasks", (
        "a real failure must still send the task back to implement")


def test_a_rewind_leaves_no_open_gate_on_an_agent_step(monkeypatch, tmp_path):
    """implement_tasks is an AGENT step. It has no gate, so gate_state must
    not read 'pending' there -- that state is invisible to
    is_open_gate_step() and let the stall handler close a task over it."""
    from prism_service.services import oracle_spec
    gr, svc, task = _arrange(monkeypatch, tmp_path, oracle_spec.ST_FAILED)

    gr.maybe_rewind(_Ctx(svc), task, "proj")

    after = svc.get(task.id)
    assert after.workflow_step == "implement_tasks"
    assert after.gate_state == "none", (
        "a non-gate step must not carry an open gate; that incoherent row is "
        f"what blinded the done-guard -- got gate_state={after.gate_state!r}")
