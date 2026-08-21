"""The verify_plan step must STATE the contract it is scored against.

Observed live on task 90ad4c39 ("Remove Simulate Flow button from
Workflows canvas"): the drive passed story_gate cleanly (thanks to
test_draft_story_step_states_its_contract.py's fix), advanced to
verify_plan, then failed plan_gate with "plan_coverage: story carries no
AC-<n> ids to diff coverage against" -- despite the story having just
been verified AC-complete one step earlier.

ROOT CAUSE: _verify_rubric_gate (conductor_service.py:3049-3051) builds
plan_coverage's evidence as {"story_md": task.plan_doc, "plan_doc":
task.plan_doc, ...} -- BOTH keys read the SAME field, deliberately,
because verify_plan's own report REPLACES plan_doc (task_runner._route_
proof routes both draft_story and verify_plan reports to the same
field). The old instruction ("Verify the plan covers the story
(plan_coverage).") never said the driver's report overwrites the very
document holding the story, so the driver wrote a short standalone
coverage note -- which wiped the story's AC-<n> lines from the only
field the rubric can read them from.

ANTI-VACUITY, same discipline as the draft_story/premise suites: every
assertion runs against the RENDERED job for a REAL task standing on
verify_plan, fetched through the same flow_next a driver actually calls.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_DOCTRINE_MARKER = "\n\nRules for this role ("

_COMPLIANT_STORY = (
    "## Summary\nRemove the Simulate Flow button.\n\n"
    "## Requirements\nNo other toolbar affordance is touched.\n\n"
    "## Acceptance Criteria\n"
    "- AC-1: the button no longer renders — oracle: navigate to "
    "/workflows and confirm it's gone\n"
    "- AC-2: the build has no unused-variable errors — oracle: "
    "`npm run build` exits 0\n"
)

_PRINCIPLES = [{
    "id": "ARC-1", "kind": "layer_rule",
    "from": "domain", "must_not_depend_on": "infrastructure",
}]


@pytest.fixture(scope="module")
def verify_plan_job():
    from prism_service import config
    from prism_service.api import conductor_flow as cf
    from prism_service.project_context import get_project, release_project

    mp = pytest.MonkeyPatch()
    tmp = tempfile.TemporaryDirectory()
    project = "verify-plan-step-contract-90ad4c39"
    try:
        mp.setattr(config, "PROJECTS_DIR", Path(tmp.name))
        release_project(project)
        ctx = get_project(project)
        task = ctx.task_svc.create(title="verify plan step contract probe")
        res = ctx.conductor_svc.advance_task(task.id)
        assert res.get("to_step") == "review_previous_notes", res
        ctx.task_svc.update(
            task.id,
            premise_notes=("## Premises\n- throwaway probe task, no prior "
                            "notes exist - UNVERIFIED\n"))
        res = ctx.conductor_svc.advance_task(task.id)
        assert res.get("to_step") == "draft_story", res
        ctx.task_svc.update(task.id, plan_doc=_COMPLIANT_STORY)
        res = ctx.conductor_svc.advance_task(task.id)
        assert res.get("to_step") == "story_gate", res
        # story_gate autoclears on a compliant story, but only via the
        # SAME machinery a real drive's flow_report triggers -- calling
        # conductor_svc.advance_task directly (above) bypasses it.
        cf._autoclear_machine_gate(ctx.conductor_svc, task.id)
        task = ctx.task_svc.get(task.id)
        assert task.workflow_step == "verify_plan", task.workflow_step
        job = cf.flow_next(task.id, project=project)["job"]
        assert job and job["step"] == "verify_plan", job
        yield {"project": project, "task_id": task.id, "job": job}
    finally:
        release_project(project)
        mp.undo()
        try:
            tmp.cleanup()
        except OSError:
            pass


def _instructions(verify_plan_job) -> str:
    return verify_plan_job["job"]["instructions"]


def _base(verify_plan_job) -> str:
    return _instructions(verify_plan_job).split(_DOCTRINE_MARKER)[0]


# ----------------------------------------------------------------------
# R1 -- the instruction must say the report REPLACES plan_doc.
# ----------------------------------------------------------------------


def test_instruction_states_the_report_replaces_plan_doc(verify_plan_job):
    low = _instructions(verify_plan_job).lower()
    assert "replaces" in low, (
        f"the instruction never says the report REPLACES plan_doc, the "
        f"exact mechanism that destroyed the story's AC ids live: {low!r}")


# ----------------------------------------------------------------------
# R2 -- the instruction must say the AC-<n> lines must survive verbatim.
# ----------------------------------------------------------------------


def test_instruction_states_ac_lines_must_survive_verbatim(verify_plan_job):
    low = _instructions(verify_plan_job).lower()
    assert "ac-<n>" in low, (
        f"the instruction never names the AC-<n> lines that must survive "
        f"into the new plan_doc: {low!r}")
    assert "verbatim" in low, (
        f"the instruction never says the AC lines must be copied "
        f"VERBATIM (not paraphrased/summarized away): {low!r}")


def test_instruction_still_names_plan_diagram(verify_plan_job):
    low = _instructions(verify_plan_job).lower()
    assert "plan_diagram" in low or "diagram" in low, (
        f"the instruction dropped the plan_diagram requirement while "
        f"being rewritten: {low!r}")


# ----------------------------------------------------------------------
# R3 -- it must land where a driver actually receives it.
# ----------------------------------------------------------------------


def test_the_rendered_instruction_is_the_guide_entry_itself(verify_plan_job):
    from prism_service.api import conductor_flow as cf

    assert _base(verify_plan_job) == cf._GUIDE["verify_plan"], (
        "the text a driver receives is not the _GUIDE entry "
        "(conductor_flow.py), so the fix was written somewhere the job "
        "payload never renders")


# ----------------------------------------------------------------------
# R4 -- END TO END, the load-bearing proof: a report written the way the
# NEW instruction tells a driver to write one (full story, AC-<n> lines
# verbatim, plus coverage) must pass the REAL scorer when fed through
# the SAME evidence shape _verify_rubric_gate actually builds (story_md
# and plan_doc as the SAME field) -- not the two-separate-fields shape
# other rubric tests use, since that shape is exactly what hid this bug.
# ----------------------------------------------------------------------


def test_a_report_written_to_the_instruction_passes_the_real_scorer_via_the_real_evidence_shape():
    from prism_service.services.arc_governance import (
        load_rubrics, score_plan_coverage)

    report = (
        _COMPLIANT_STORY
        + "\n## Plan Coverage\n"
        + "- AC-1: covered by removing the JSX button in WorkflowsPage.tsx\n"
        + "- AC-2: covered by running npm run build after the removal\n"
    )
    evidence = {
        "story_md": report,   # the SAME field, matching _verify_rubric_gate
        "plan_doc": report,
        "plan_diagram": "flowchart TD\n  toolbar --> button\n",
    }
    res = score_plan_coverage(
        evidence, load_rubrics()["plan_coverage"], _PRINCIPLES)
    assert res["ok"] is True, res


def test_the_live_failure_still_reproduces_and_the_gate_stays_strict():
    """A report that drops the story's AC lines (the old failure mode)
    must still be refused -- the fix is a better instruction, never a
    loosened rubric."""
    from prism_service.services.arc_governance import (
        load_rubrics, score_plan_coverage)

    stripped_report = "## Plan Coverage\n- covers the story's requirements\n"
    evidence = {
        "story_md": stripped_report,
        "plan_doc": stripped_report,
        "plan_diagram": "flowchart TD\n  toolbar --> button\n",
    }
    res = score_plan_coverage(
        evidence, load_rubrics()["plan_coverage"], _PRINCIPLES)
    assert res["ok"] is False
    assert "no AC-<n> ids" in res["reason"] or "AC" in res["reason"], res


# ----------------------------------------------------------------------
# R5 -- reaches a driver over the wire.
# ----------------------------------------------------------------------


def test_the_contract_reaches_a_driver_over_the_http_wire(verify_plan_job):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    client = TestClient(app)
    resp = client.get(
        "/api/conductor/flow/next",
        params={"task_id": verify_plan_job["task_id"],
                "project": verify_plan_job["project"]})
    assert resp.status_code == 200, resp.text
    job = resp.json()["job"]
    assert job["step"] == "verify_plan", job
    low = job["instructions"].lower()
    for needle in ("replaces", "ac-<n>", "verbatim"):
        assert needle in low, (needle, job["instructions"])


# ----------------------------------------------------------------------
# R6 -- this slice must not edit its own gate policy.
# ----------------------------------------------------------------------

SLICE_FILES = (
    "services/prism-service/prism_service/api/conductor_flow.py",
    "services/prism-service/prism_service/__version__.py",
    "services/prism-service/tests/unit/"
    "test_verify_plan_step_states_its_contract.py",
)


def test_no_file_in_this_slice_is_a_gate_policy_file():
    from prism_service.services.control_plane import POLICY_FILES

    overlap = sorted(set(SLICE_FILES) & set(POLICY_FILES))
    assert not overlap, (
        "this slice edits its own judge; move the fix out of POLICY_FILES "
        f"or the candidate-controls-judge tooth blocks the gate: {overlap}")
