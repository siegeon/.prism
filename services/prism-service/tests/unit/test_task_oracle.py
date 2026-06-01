"""Oracle field (ported from goalbuddy) — upfront observable completion signal.

Pins: oracle/proof_type/completion_proof round-trip on the Task model + DB,
the is_weak_proof() guard (ported from goalbuddy check-goal-state.mjs), and
the advisory green_gate annotation when a task closes with no real proof.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc
    )
    return task_svc, cond


# ---- field round-trip -------------------------------------------------

def test_oracle_fields_default_empty(tmp_path):
    task_svc, _ = _services(tmp_path)
    t = task_svc.create(title="x")
    assert (t.oracle, t.proof_type, t.completion_proof) == ("", "", "")
    g = task_svc.get(t.id)
    assert (g.oracle, g.proof_type, g.completion_proof) == ("", "", "")


def test_oracle_fields_round_trip_create_and_update(tmp_path):
    task_svc, _ = _services(tmp_path)
    t = task_svc.create(
        title="x", oracle="curl /api/foo returns 200", proof_type="demo"
    )
    assert t.oracle == "curl /api/foo returns 200"
    assert t.proof_type == "demo"
    task_svc.update(t.id, completion_proof="ran curl, got 200 (screenshot)")
    g = task_svc.get(t.id)
    assert g.oracle == "curl /api/foo returns 200"
    assert g.proof_type == "demo"
    assert g.completion_proof == "ran curl, got 200 (screenshot)"


# ---- is_weak_proof (goalbuddy port) -----------------------------------

def test_is_weak_proof_matches_goalbuddy_semantics():
    from prism_service.services.conductor_service import is_weak_proof

    for weak in [None, "", "   ", "unknown", "TBD", "todo", "None",
                 "<observable signal>", "<fill me>"]:
        assert is_weak_proof(weak) is True, weak
    for real in ["532 tests pass", "demo recorded at /x.mp4", "0"]:
        assert is_weak_proof(real) is False, real


# ---- green_gate advisory annotation -----------------------------------

def _drive_to_green_gate(task_svc, cond, t):
    guard = 0
    while task_svc.get(t.id).workflow_step != "green_gate" and guard < 20:
        step = task_svc.get(t.id).workflow_step
        gate = task_svc.get(t.id).gate_state
        if gate == "pending":
            cond.gate_decide(t.id, "approve", reason="ok", override=True)
        else:
            cond.advance_task(t.id, validation="step")
        guard += 1
    assert task_svc.get(t.id).workflow_step == "green_gate"


def test_green_gate_flags_missing_completion_proof(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="no proof")  # completion_proof == ""
    _drive_to_green_gate(task_svc, cond, t)
    cond.gate_decide(t.id, "approve", reason="full green", override=True)
    assert "no completion_proof recorded" in task_svc.get(t.id).gate_reason


def test_green_gate_clean_when_proof_present(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="has proof",
                        completion_proof="532 tests pass + demo")
    _drive_to_green_gate(task_svc, cond, t)
    cond.gate_decide(t.id, "approve", reason="full green", override=True)
    assert "no completion_proof recorded" not in task_svc.get(t.id).gate_reason
