"""The draft_story step must STATE the contract it is scored against.

Observed live, twice, on two separately-driven real tasks (785022b3 then
b9772333, both "Remove Simulate Flow button from Workflows canvas"): the
autonomous drive reached story_gate and was refused BOTH times --
"no acceptance criteria with AC-<n> ids found", then on the retry
"missing required section(s): Summary, Requirements, Acceptance Criteria"
-- despite the instruction the driver actually received naming the right
CONCEPTS ("Summary/Requirements/Acceptance Criteria with AC ids +
oracles"). The instruction never said the story_complete rubric
(arc_governance.py:110-143 / governance_rubrics.yaml:9-22) needs the
section names as literal markdown HEADINGS, the literal 'AC-<n>' token,
or the literal 'oracle:' marker on (or directly under) each AC bullet --
so a reasonable-sounding story kept failing the mechanical parser.

ANTI-VACUITY, same discipline as test_premise_step_states_its_contract.py:
every assertion below runs against the RENDERED job for a REAL task
standing on draft_story, fetched through the same flow_next a driver
actually calls -- never the _GUIDE dict literal in isolation.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_DOCTRINE_MARKER = "\n\nRules for this role ("


@pytest.fixture(scope="module")
def draft_story_job():
    from prism_service import config
    from prism_service.api import conductor_flow as cf
    from prism_service.project_context import get_project, release_project

    mp = pytest.MonkeyPatch()
    tmp = tempfile.TemporaryDirectory()
    project = "draft-story-step-contract-785022b3"
    try:
        mp.setattr(config, "PROJECTS_DIR", Path(tmp.name))
        release_project(project)
        ctx = get_project(project)
        task = ctx.task_svc.create(title="draft story step contract probe")
        res = ctx.conductor_svc.advance_task(task.id)
        assert res.get("to_step") == "review_previous_notes", res
        # review_previous_notes carries its own inline check_at_step
        # (premise_grounded) that must PASS before advance_task lets a
        # second call move past it -- seed a compliant premise report so
        # this fixture reaches draft_story the same way a real driver does.
        ctx.task_svc.update(
            task.id,
            premise_notes=("## Premises\n- this is a throwaway probe task, "
                            "no prior notes exist - UNVERIFIED\n"))
        res = ctx.conductor_svc.advance_task(task.id)
        assert res.get("to_step") == "draft_story", res
        job = cf.flow_next(task.id, project=project)["job"]
        assert job and job["step"] == "draft_story", job
        yield {"project": project, "task_id": task.id, "job": job}
    finally:
        release_project(project)
        mp.undo()
        try:
            tmp.cleanup()
        except OSError:
            pass


def _instructions(draft_story_job) -> str:
    return draft_story_job["job"]["instructions"]


def _base(draft_story_job) -> str:
    """The step's OWN instruction: the doctrine splice stripped off the
    end, and the universal final-message-caveat prefix (_job() prepends
    it to every non-gate step) stripped off the start."""
    from prism_service.api import conductor_flow as cf

    text = _instructions(draft_story_job)
    prefix = cf._FINAL_MESSAGE_CAVEAT + "\n\n"
    if text.startswith(prefix):
        text = text[len(prefix):]
    return text.split(_DOCTRINE_MARKER)[0]


# ----------------------------------------------------------------------
# R1 -- the exact required section names, taken from the rubric itself.
# ----------------------------------------------------------------------


def test_instruction_names_every_required_section(draft_story_job):
    from prism_service.services.arc_governance import load_rubrics

    required = load_rubrics()["story_complete"]["required_sections"]
    assert required == ["Summary", "Requirements", "Acceptance Criteria"], (
        "story_complete's required_sections moved; this suite pins the "
        f"names the driver is told to write: {required}")
    text = _instructions(draft_story_job)
    for section in required:
        assert section in text, (
            f"the draft_story instruction never names the {section!r} "
            f"section story_complete requires: {text!r}")


# ----------------------------------------------------------------------
# R2 -- the AC id syntax and the oracle marker, literally.
# ----------------------------------------------------------------------


def test_instruction_states_the_ac_id_pattern(draft_story_job):
    text = _instructions(draft_story_job)
    assert "AC-" in text, (
        f"the instruction never shows the literal 'AC-' id token the "
        f"parser looks for (arc_governance.py:87-105): {text!r}")
    assert re.search(r"AC-1", text), (
        "the instruction never gives a concrete numbered example "
        f"(AC-1, AC-2, ...): {text!r}")


def test_instruction_states_the_oracle_marker(draft_story_job):
    low = _instructions(draft_story_job).lower()
    assert "oracle:" in low, (
        "the instruction never states the literal 'oracle:' marker "
        f"score_story_complete requires on each AC: {low!r}")


# ----------------------------------------------------------------------
# R3 -- the consequence, same discipline as the premise step.
# ----------------------------------------------------------------------


def test_instruction_states_the_report_is_refused(draft_story_job):
    low = _instructions(draft_story_job).lower()
    assert "refus" in low, (
        f"the instruction never says a non-compliant story is REFUSED: {low!r}")
    assert "advance" in low, (
        f"the instruction never says the step will not advance: {low!r}")


# ----------------------------------------------------------------------
# R4 -- it must land where a driver actually receives it.
# ----------------------------------------------------------------------


def test_the_rendered_instruction_is_the_guide_entry_itself(draft_story_job):
    from prism_service.api import conductor_flow as cf

    assert _base(draft_story_job) == cf._GUIDE["draft_story"], (
        "the text a driver receives is not the _GUIDE entry "
        "(conductor_flow.py), so the fix was written somewhere the job "
        "payload never renders")


# ----------------------------------------------------------------------
# R5 -- END TO END, the load-bearing proof: a story written exactly the
# way the NEW instruction tells a driver to write one must actually PASS
# the REAL scorer. This is what "state the contract" is FOR -- an
# instruction that reads precisely but still doesn't satisfy its own
# rubric would reproduce the exact live failure this test exists to close.
# ----------------------------------------------------------------------


def test_a_story_written_to_the_instruction_passes_the_real_scorer():
    from prism_service.services.arc_governance import (
        load_rubrics, score_story_complete)

    compliant = (
        "## Summary\n"
        "Remove the Simulate Flow button from the Workflows toolbar.\n\n"
        "## Requirements\n"
        "The button and its dead handlers are removed; no other toolbar "
        "affordance is touched.\n\n"
        "## Acceptance Criteria\n"
        "- AC-1: the Simulate Flow button no longer renders in the "
        "toolbar — oracle: navigate to /workflows and confirm it's gone\n"
        "- AC-2: the build has no unused-variable errors — oracle: "
        "`npm run build` exits 0\n"
    )
    res = score_story_complete(
        {"story_md": compliant}, load_rubrics()["story_complete"])
    assert res["ok"] is True, res


# ----------------------------------------------------------------------
# R6 -- the two LIVE failures this fix closes must genuinely reproduce
# against the real scorer (so this suite is pinned to the actual observed
# bug, not a guess at it), and must stay red -- the fix is a better
# INSTRUCTION, never a loosened rubric.
# ----------------------------------------------------------------------


@pytest.mark.parametrize("story,expected_substring", [
    ("## Summary\nok\n\n## Requirements\nok\n\n## Acceptance Criteria\n"
     "no ids here, just prose\n", "no acceptance criteria with AC-<n>"),
    ("## Acceptance Criteria\n- AC-1: x — oracle: y\n",
     "missing required section"),
])
def test_the_two_live_failures_still_reproduce_and_the_gate_stays_strict(
    story, expected_substring,
):
    from prism_service.services.arc_governance import (
        load_rubrics, score_story_complete)

    res = score_story_complete(
        {"story_md": story}, load_rubrics()["story_complete"])
    assert res["ok"] is False
    assert expected_substring in res["reason"], res


# ----------------------------------------------------------------------
# R7 -- reaches a driver over the wire, same path conductor_work/task_runner
# actually use.
# ----------------------------------------------------------------------


def test_the_contract_reaches_a_driver_over_the_http_wire(draft_story_job):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    client = TestClient(app)
    resp = client.get(
        "/api/conductor/flow/next",
        params={"task_id": draft_story_job["task_id"],
                "project": draft_story_job["project"]})
    assert resp.status_code == 200, resp.text
    job = resp.json()["job"]
    assert job["step"] == "draft_story", job
    text = job["instructions"]
    for needle in ("Summary", "Requirements", "Acceptance Criteria", "AC-1"):
        assert needle in text, (needle, text)
    assert "oracle:" in text.lower()


# ----------------------------------------------------------------------
# R8 -- this slice must not edit its own gate policy.
# ----------------------------------------------------------------------

SLICE_FILES = (
    "services/prism-service/prism_service/api/conductor_flow.py",
    "services/prism-service/prism_service/__version__.py",
    "services/prism-service/tests/unit/"
    "test_draft_story_step_states_its_contract.py",
)


def test_no_file_in_this_slice_is_a_gate_policy_file():
    from prism_service.services.control_plane import POLICY_FILES

    overlap = sorted(set(SLICE_FILES) & set(POLICY_FILES))
    assert not overlap, (
        "this slice edits its own judge; move the fix out of POLICY_FILES "
        f"or the candidate-controls-judge tooth blocks the gate: {overlap}")
