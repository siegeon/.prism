"""Every agent step's job must warn that the driver's LAST message becomes
the official step report, verbatim -- gates never receive it.

Observed live on task 88d79ca9: verify_plan's real driving session wrote a
compliant plan_doc (with the story's AC-<n> ids intact, per 7.12.31's fix)
via its own tool calls mid-session, kept reasoning for more turns, and
ended on a conversational sign-off ("Want me to approve plan_gate on your
behalf...?"). task_runner._route_proof writes claude_cli.invoke()'s
final_text() as the step's report unconditionally, so that sign-off
silently REPLACED the compliant plan_doc the driver had already written.
Neither the draft_story nor verify_plan instruction fixes (7.12.30,
7.12.31) said anything about this -- they specified the CONTENT format,
never that the driver's FINAL message specifically is what's captured.

Exercises `_job()` directly against a minimal fake task -- the function
only ever reads workflow_step/gate_state plus a few getattr-guarded
fields, so a real task_svc/project isn't needed to pin its OUTPUT SHAPE
for every step id in WORKFLOW_STEPS.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_CAVEAT_NEEDLE = "your last message becomes this step's official report"


def _fake_task(workflow_step: str):
    return SimpleNamespace(
        id="fake-task-id", workflow_step=workflow_step, gate_state="none",
        gate_reason="", allowed_files=[], verify=[], stop_if=[])


def _job_for(step_id: str) -> dict:
    from prism_service.api import conductor_flow as cf

    job = cf._job(_fake_task(step_id))
    assert job is not None, f"_job() returned None for a real step id {step_id!r}"
    return job


_AGENT_STEPS = ("review_previous_notes", "draft_story", "verify_plan",
                "write_failing_tests", "implement_tasks", "verify_green_state")
_GATE_STEPS = ("story_gate", "plan_gate", "red_gate", "green_gate")


@pytest.mark.parametrize("step_id", _AGENT_STEPS)
def test_agent_step_carries_the_final_message_caveat(step_id):
    job = _job_for(step_id)
    assert job["kind"] != "gate", job
    low = job["instructions"].lower()
    assert _CAVEAT_NEEDLE in low, (
        f"{step_id!r}'s job never warns that the driver's LAST message "
        f"becomes the official report: {job['instructions']!r}")


@pytest.mark.parametrize("step_id", _GATE_STEPS)
def test_gate_step_does_not_carry_the_caveat(step_id):
    job = _job_for(step_id)
    assert job["kind"] == "gate", job
    low = job["instructions"].lower()
    assert _CAVEAT_NEEDLE not in low, (
        f"{step_id!r} is a GATE job but carries the driver final-message "
        f"caveat, even though gates are never driven by claude_cli at "
        f"all: {job['instructions']!r}")


def test_no_file_in_this_slice_is_a_gate_policy_file():
    from prism_service.services.control_plane import POLICY_FILES

    slice_files = (
        "services/prism-service/prism_service/api/conductor_flow.py",
        "services/prism-service/prism_service/__version__.py",
        "services/prism-service/tests/unit/test_step_final_message_caveat.py",
    )
    overlap = sorted(set(slice_files) & set(POLICY_FILES))
    assert not overlap, (
        "this slice edits its own judge; move the fix out of POLICY_FILES "
        f"or the candidate-controls-judge tooth blocks the gate: {overlap}")
