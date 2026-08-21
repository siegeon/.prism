"""implement_tasks' instruction must tell the driver to COMMIT its change.

Observed live on task 4f74dafc ("Remove Simulate Flow button from
Workflows canvas"): the drive reached green_gate and status=done with the
ACTUAL implementation sitting as an uncommitted working-tree diff in the
task's own workspace -- never committed, never merged, never shippable.
write_failing_tests correctly committed its tests-only [task:<id>] commit
(CLAUDE.md already documents that convention), but neither
ROLE_RULES["dev"] (context_builder.py) nor the old implement_tasks
instruction ("Smallest change that turns the failing tests green.") said
anything about committing at all, and verify_green_state's tests can pass
against an uncommitted worktree just fine -- nothing downstream catches
this before green_gate.

Exercises _job() directly against a minimal fake task, same approach as
test_step_final_message_caveat.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _job_for(step_id: str) -> dict:
    from prism_service.api import conductor_flow as cf

    task = SimpleNamespace(
        id="fake-task-id", workflow_step=step_id, gate_state="none",
        gate_reason="", allowed_files=[], verify=[], stop_if=[])
    job = cf._job(task)
    assert job is not None, f"_job() returned None for a real step id {step_id!r}"
    return job


def test_implement_tasks_instruction_says_commit():
    job = _job_for("implement_tasks")
    low = job["instructions"].lower()
    assert "commit" in low, (
        f"implement_tasks's job never tells the driver to commit its "
        f"change: {job['instructions']!r}")


def test_implement_tasks_instruction_names_the_task_trailer_convention():
    job = _job_for("implement_tasks")
    low = job["instructions"].lower()
    assert "[task:<task_id>]" in low or "[task:" in low, (
        f"implement_tasks's job never names the [task:<id>] commit "
        f"trailer convention used elsewhere in this drive (the red-step "
        f"tests commit): {job['instructions']!r}")


def test_implement_tasks_instruction_says_uncommitted_is_not_shippable():
    job = _job_for("implement_tasks")
    low = job["instructions"].lower()
    assert "uncommitted" in low or "not shippable" in low, (
        f"implement_tasks's job never states the CONSEQUENCE of skipping "
        f"the commit (an uncommitted diff is not shippable): "
        f"{job['instructions']!r}")


def test_no_file_in_this_slice_is_a_gate_policy_file():
    from prism_service.services.control_plane import POLICY_FILES

    slice_files = (
        "services/prism-service/prism_service/api/conductor_flow.py",
        "services/prism-service/prism_service/__version__.py",
        "services/prism-service/tests/unit/"
        "test_implement_tasks_commit_instruction.py",
    )
    overlap = sorted(set(slice_files) & set(POLICY_FILES))
    assert not overlap, (
        "this slice edits its own judge; move the fix out of POLICY_FILES "
        f"or the candidate-controls-judge tooth blocks the gate: {overlap}")
