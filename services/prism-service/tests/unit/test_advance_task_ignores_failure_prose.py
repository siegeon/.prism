"""advance_task must judge a step by its TYPED outcome, never by the
wording of completion_proof.

SUPERSEDES tests/unit/test_advance_task_refuses_failed_steps.py (96a34c56,
task 0a9c3d1d), which pinned the opposite contract: refuse to advance any
`agent` step whose completion_proof contained "failed", "error", "cannot",
"could not", "exhausted", "permission denied" or "did not land".

That guard refused 298 of the 569 stored proofs in this project's own
tasks.db, 245 of them on tasks that legitimately reached done — because the
correct proof of a PASSING write_failing_tests step is real pytest output
("4 failed, 3 passed"), and a verify_green_state proof says "0 failed".
Its own green case only passed by accident: it used the word "failing",
which does not contain "failed".

Narrowing the marker list does not rescue it. At the tightest useful set
("permission denied", "budget exhausted", "did not land"), 2 of the 3 live
matches are still successful steps whose proof DESCRIBES failure handling.
PRISM's domain vocabulary is failure vocabulary.

The typed channel already exists: api/conductor_flow.py `_is_failure`
leaves an agent step where it stands on a reported failure.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services.conductor_service import ConductorService


class FakeTaskService:
    def __init__(self, task):
        self._task = task
        self.history: list[dict] = []

    def get(self, task_id):
        return self._task

    def update(self, task_id, **kwargs):
        for k, v in kwargs.items():
            setattr(self._task, k, v)

    def record_history(self, task_id, **kwargs):
        self.history.append(kwargs)


def _make_task(workflow_step="", completion_proof="", **kwargs):
    defaults = {
        "id": "test-task",
        "title": "test",
        "workflow_step": workflow_step,
        "completion_proof": completion_proof,
        "gate_state": "none",
        "gate_reason": "",
        "workflow": "implement",
        "parent_id": "",
        "status": "in_progress",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _advance(proof, step="write_failing_tests"):
    task = _make_task(workflow_step=step, completion_proof=proof)
    svc = FakeTaskService(task)
    conductor = ConductorService(":memory:", task_svc=svc)
    return conductor.advance_task("test-task"), svc


# The literal proof text a CORRECT write_failing_tests step produces: the
# step's whole job is to leave tests failing, so its evidence says "failed".
REAL_RED_PROOF = (
    "Committed tests-only [task:test-task] at abc123d. "
    "pytest tests/unit/test_thing.py: 4 failed, 3 passed in 2.11s (rc=1); "
    "the red anchor is that commit."
)

REAL_GREEN_PROOF = (
    "pytest tests/unit/test_thing.py: 7 passed, 0 failed in 1.98s (rc=0). "
    "No errors in the daemon log; tsc --noEmit clean."
)

# A successful step whose proof DESCRIBES failure handling — the shape that
# defeats even a narrowed marker list (live examples b612fa19, 6e37bcdc).
DESCRIBES_FAILURE_PROOF = (
    "Implemented the auto-rewind ceiling: on exhaustion the drive parks with "
    "gate_reason 'auto-rewind budget exhausted' instead of bouncing. The "
    "release daemon update was intentionally NOT performed (permission "
    "denied by classifier, per policy)."
)


@pytest.mark.parametrize("proof", [
    REAL_RED_PROOF, REAL_GREEN_PROOF, DESCRIBES_FAILURE_PROOF,
])
def test_advance_is_not_refused_over_proof_wording(proof):
    """No refusal may cite the wording of completion_proof."""
    result, svc = _advance(proof)

    reason = str(result.get("reason", "")).lower()
    assert "completion_proof" not in reason, (
        f"advance refused over proof wording: {result.get('reason')!r}")
    assert "failure indicator" not in reason

    refusals = [r for r in svc.history
                if r.get("action") == "advance_refused"
                and "completion-proof-failed" in str(r.get("details", ""))]
    assert refusals == [], (
        "no advance may be refused on a completion-proof substring match")


def test_the_red_step_advances_on_its_own_failing_test_output():
    """The step whose correct evidence is 'N failed' must still advance."""
    result, _ = _advance(REAL_RED_PROOF)

    assert result["ok"] is True, (
        f"write_failing_tests blocked by its own red evidence: {result}")
    assert result["to_step"] != "write_failing_tests"


def test_empty_completion_proof_does_not_refuse():
    result, svc = _advance("")

    assert "completion_proof" not in str(result.get("reason", "")).lower()
    assert not [r for r in svc.history
                if "completion-proof-failed" in str(r.get("details", ""))]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
