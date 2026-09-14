"""Readiness says who must act on a refused gate - task 5c61e0e6.

DEFECT: GET /api/conductor/gate/readiness (api/conductor.py, gate_readiness),
red_gate branch, unconditionally returns the optimistic placeholder ("no RED
receipt yet ... no owner action needed") whenever ``osp.fresh_red_receipt``
finds nothing - even when the machine seat already ran ``run_red_oracle`` at
this exact red_sha+spec, got a NOT-red verdict (pinned suite PASSES at the
immutable red-step anchor: ``osp.ST_FAILED``, reason "NOT red: ..."), and
will never retry it (``adjudicate_test_red_gate``'s own ``tried`` guard,
conductor_service.py:2362-2367, abstains forever once ANY receipt exists at
that anchor+spec). ``fresh_red_receipt`` only matches ``status == ST_RED``
(oracle_spec.py:452), so a swept-and-refused receipt is invisible to the one
check readiness makes, and a driver polling readiness waits on a sweep that
structurally cannot come.

  AC-1  a red_gate whose anchor+spec already has a receipt on file with a
        NOT-red verdict (status != ST_RED) -> receipt_ok:false, the refusal
        names the red-step commit + the seat's actual stored reason, says NO
        future sweep will decide it, and says the gate needs a distinct
        actor's decision NOW (never "no owner action needed").
  AC-2  anti-regression: the SAME shape (proof_type=test, spec present,
        red_sha present, machine seat enabled) but with NO receipt at all on
        file for that anchor+spec reads exactly as today - pending, no owner
        call to action - because a sweep genuinely has not happened yet.

SUPERSEDED IN PART (task bb3d1f6a, 2026-09-13): AC-1's premise -- "no
machine path exists, ever" -- stopped being true the moment
plan_rewind.maybe_rewind (task 1bcb2b24) shipped a red_gate backward edge:
a "NOT red: ... PASS" verdict (and, after this same task, a COLLECTION
ERROR verdict too -- see oracle_spec.red_refusal_kind) is now something the
machine seat redrafts on its own, via write_failing_tests, on its next
sweep. Continuing to say "needs a distinct actor's decision" for that case
would be the SAME dishonesty this file was written to fix, aimed the other
direction: it would tell a driver to fetch a human for a gate the machine
is already about to retry. AC-1 below now asserts the machine-retry-in-
flight wording for the two REWINDABLE causes, and a new
test_budget_exhausted_swept_gate_names_the_owner_action covers the case
AC-1 originally meant -- once the rewind budget is spent, a human really is
the only path left, and that message is unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import control_plane as cp  # noqa: E402
from prism_service.services import oracle_spec as osp  # noqa: E402
from prism_service.services.conductor_service import (  # noqa: E402
    _red_pytest_spec)

_RED_SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    cond._project_name = "default"
    return task_svc, cond


def _wire(monkeypatch, cond, capi, red_sha=_RED_SHA,
          adjudicator_enabled=True):
    """Bypass the dirty-judge check (unrelated to this fix) and pin
    ``_red_step_sha``/the adjudicator switch to known values so the
    red_gate branch under test is reached deterministically."""
    monkeypatch.setattr(cp, "dirty_judge_reason", lambda: "")
    monkeypatch.setattr(capi, "_svc", lambda project: cond)
    monkeypatch.setattr(cond, "_red_step_sha", lambda task_id: red_sha)
    from prism_service.services import gate_adjudicator as ga
    monkeypatch.setattr(ga, "is_enabled", lambda: adjudicator_enabled)


def _red_gated_task(task_svc):
    t = task_svc.create(title="red-gate task", tags=["conductor"],
                        oracle="pytest tests/unit/test_x.py::test_foo -q",
                        proof_type="test")
    task_svc.update(t.id, verify=["tests/unit/test_x.py::test_foo"],
                    workflow_step="red_gate", gate_state="pending")
    return task_svc.get(t.id)


# ---------------------------------------------------------------------------
# AC-1 - swept and refused: readiness must name the owner action, not stall
# ---------------------------------------------------------------------------


def _mk_receipt(spec, reason, rc, ended_at="2026-08-17T00:00:00Z"):
    return osp.EvidenceReceipt(
        task_id="", job_id="swept-job-1", spec_hash=spec.spec_hash(),
        tree_sha=_RED_SHA, adapter=spec.adapter, passed=False,
        status=osp.ST_FAILED, ended_at=ended_at, reason=reason,
        observations=[{"name": "pytest_pass", "observed": rc}])


def test_swept_and_refused_red_gate_names_the_owner_action(
        tmp_path, monkeypatch):
    """AC-1, UPDATED (task bb3d1f6a): a "NOT red: ... PASS" verdict is now a
    REWINDABLE cause (plan_rewind.maybe_rewind, task 1bcb2b24) -- the
    machine seat redrafts it via write_failing_tests on its own next sweep,
    so readiness must say a machine retry is in flight, never claim a human
    is needed for a gate the machine is already about to retry. The
    budget-exhausted case (where a human genuinely is the only path left)
    is covered separately below."""
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    _wire(monkeypatch, cond, capi)

    task = _red_gated_task(task_svc)
    spec = _red_pytest_spec(task)
    assert spec is not None, "sanity: pinned verify must derive a pytest spec"

    # The seat's ACTUAL persisted verdict: it ran run_red_oracle at this
    # anchor, the pinned suite PASSED there, and it recorded a NOT-red
    # receipt (osp.ST_FAILED) - exactly what adjudicate_test_red_gate
    # persists before abstaining (conductor_service.py:2372-2377).
    refusal_reason = (
        f"NOT red: the spec's tests PASS at the red-step commit "
        f"{_RED_SHA[:12]} (pytest_ids: tests/unit/test_x.py::test_foo -> "
        "rc=0 (1 passed))")
    receipt = _mk_receipt(spec, refusal_reason, rc=0)
    receipt.task_id = task.id
    osp.append_receipt("default", receipt)

    out = capi.gate_readiness(task_id=task.id, project="default")

    assert out["receipt_ok"] is False, out
    refusal = out["receipt_refusal"]
    # names the red-step commit and quotes the seat's actual reason
    assert _RED_SHA[:12] in refusal, refusal
    assert "NOT red" in refusal, refusal
    assert "PASS at the red-step commit" in refusal, refusal
    # a fresh (never-rewound) task must say the MACHINE is retrying, not
    # send a driver to fetch a human for something already in flight
    assert "no owner action needed" in refusal, refusal
    assert "redrafting" in refusal.lower(), refusal
    assert "next sweep" in refusal, refusal
    assert "distinct actor" not in refusal.lower(), refusal


def test_budget_exhausted_swept_gate_names_the_owner_action(
        tmp_path, monkeypatch):
    """The case the original AC-1 meant: once plan_rewind's budget is
    spent redrafting this exact anchor, the machine truly has no path left
    and a distinct actor's decision is the honest answer again."""
    from prism_service.api import conductor as capi
    from prism_service.services import plan_rewind
    task_svc, cond = _services(tmp_path)
    _wire(monkeypatch, cond, capi)

    task = _red_gated_task(task_svc)
    spec = _red_pytest_spec(task)
    assert spec is not None

    refusal_reason = (
        f"NOT red: the spec's tests PASS at the red-step commit "
        f"{_RED_SHA[:12]} (pytest_ids: tests/unit/test_x.py::test_foo -> "
        "rc=0 (1 passed))")
    receipt = _mk_receipt(spec, refusal_reason, rc=0)
    receipt.task_id = task.id
    osp.append_receipt("default", receipt)

    budget = plan_rewind.rewind_budget("default")
    for n in range(budget):
        task_svc.record_history(
            task.id, action=plan_rewind.REWIND_ACTION,
            details=f"red_gate -> write_failing_tests; attempt={n + 1}",
            actor=plan_rewind.REWIND_ACTOR)

    out = capi.gate_readiness(task_id=task.id, project="default")

    assert out["receipt_ok"] is False, out
    refusal = out["receipt_refusal"]
    assert _RED_SHA[:12] in refusal, refusal
    assert "no owner action needed" not in refusal, refusal
    assert "on its next sweep" not in refusal, refusal
    assert ("distinct actor" in refusal.lower()
           or "owner" in refusal.lower()), refusal
    assert "budget" in refusal.lower(), refusal


def test_collection_error_swept_gate_also_shows_a_machine_retry(
        tmp_path, monkeypatch):
    """The live bb3d1f6a shape: pytest never collected a single pinned id
    (rc=4). This must read exactly like the suite-PASSES cause -- a machine
    retry in flight -- never "the pinned suite passes there" (false: nothing
    was even collected) and never a permanent human dead-end."""
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    _wire(monkeypatch, cond, capi)

    task = _red_gated_task(task_svc)
    spec = _red_pytest_spec(task)
    assert spec is not None

    refusal_reason = (
        f"red not demonstrated at {_RED_SHA[:12]} (rc=4, wanted rc==1 test "
        "failures): pytest_ids: could not collect "
        "tests/unit/test_x.py::test_foo")
    receipt = _mk_receipt(spec, refusal_reason, rc=4)
    receipt.task_id = task.id
    osp.append_receipt("default", receipt)

    out = capi.gate_readiness(task_id=task.id, project="default")

    assert out["receipt_ok"] is False, out
    refusal = out["receipt_refusal"]
    assert "could not collect" in refusal, refusal
    assert "the pinned suite passes there" not in refusal, (
        "false: nothing was even collected -- must not claim a pass")
    assert "no owner action needed" in refusal, refusal
    assert "distinct actor" not in refusal.lower(), refusal


# ---------------------------------------------------------------------------
# AC-2 - anti-regression: genuinely unswept gate stays pending, no alarm
# ---------------------------------------------------------------------------


def test_unswept_gate_still_reads_pending_with_no_owner_action(
        tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    _wire(monkeypatch, cond, capi)

    task = _red_gated_task(task_svc)
    spec = _red_pytest_spec(task)
    assert spec is not None

    # NO receipt at all recorded for this red_sha+spec_hash - the seat has
    # genuinely not swept it yet. read_receipts must be empty here so the
    # AC-1 branch this task adds cannot fire.
    assert not [r for r in osp.read_receipts("default", task.id)
               if r.tree_sha == _RED_SHA and r.spec_hash == spec.spec_hash()]

    out = capi.gate_readiness(task_id=task.id, project="default")

    assert out["receipt_ok"] is False, out
    refusal = out["receipt_refusal"]
    assert _RED_SHA[:12] in refusal, refusal
    assert "no owner action needed" in refusal, refusal
    assert "NOT red" not in refusal, refusal
