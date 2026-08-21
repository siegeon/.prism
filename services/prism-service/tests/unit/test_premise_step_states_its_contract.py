"""The premise step must STATE the contract it is scored against (task
53d756a3).

`review_previous_notes` is refused by the `premise_grounded` rubric
(`prism_service/services/arc_governance.py:286-337`) unless the report opens a
'## Premises' section whose every claim bullet carries a citation or an
explicit UNVERIFIED/REFUTED marker. The driver-visible instruction for that
step says none of it — today it reads, in full, "Review prior notes/decisions
for this task." (`prism_service/api/conductor_flow.py:35`), so a first-pass
driver is refused by a rule it was never told.

ANTI-VACUITY (the load-bearing constraint). The instruction a driver reads is
NOT the source literal: `_job()` (`conductor_flow.py:49-97`) picks the `_GUIDE`
entry at :57-58 and splices role doctrine at :65-70 before emitting it as
`job["instructions"]` at :83. Every assertion below therefore runs against the
RENDERED job for a REAL task standing on review_previous_notes, fetched through
the same functions the MCP verb calls (`mcp/tools.py:4441` imports this module;
:4476/:4506 call flow_start, and GET /api/conductor/flow/next is the identical
read) — never a docstring, never a comment, never the dict literal.

FAILS today: the rendered instruction names no section, no citation form, no
escape marker and no refusal.
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


# ----------------------------------------------------------------------
# Fixture — a REAL task standing on review_previous_notes in a throwaway
# project, and the job a driver is handed there. No worktree is created
# (flow_next is the read-only twin of flow_start's return), so this stays
# a unit test while still exercising the production call path.
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def premise_job():
    from prism_service import config
    from prism_service.api import conductor_flow as cf
    from prism_service.project_context import get_project, release_project

    mp = pytest.MonkeyPatch()
    tmp = tempfile.TemporaryDirectory()
    project = "premise-step-contract-53d756a3"
    try:
        mp.setattr(config, "PROJECTS_DIR", Path(tmp.name))
        release_project(project)
        ctx = get_project(project)
        task = ctx.task_svc.create(title="premise step contract probe")
        res = ctx.conductor_svc.advance_task(task.id)
        assert res.get("to_step") == "review_previous_notes", res
        job = cf.flow_next(task.id, project=project)["job"]
        assert job and job["step"] == "review_previous_notes", job
        yield {"project": project, "task_id": task.id, "job": job}
    finally:
        release_project(project)
        mp.undo()
        try:
            tmp.cleanup()
        except OSError:
            pass


def _instructions(premise_job) -> str:
    return premise_job["job"]["instructions"]


def _base(premise_job) -> str:
    """The step's OWN instruction, with the role-doctrine splice removed
    and the universal final-message-caveat prefix (_job() prepends it to
    every non-gate step, added after this test was first written)
    stripped off the start."""
    from prism_service.api import conductor_flow as cf

    text = _instructions(premise_job)
    prefix = cf._FINAL_MESSAGE_CAVEAT + "\n\n"
    if text.startswith(prefix):
        text = text[len(prefix):]
    return text.split(_DOCTRINE_MARKER)[0]


# ----------------------------------------------------------------------
# R1 — the SECTION NAME, taken from the rubric itself so a rename of
# claims_section (governance_rubrics.yaml:42) can never drift from what the
# driver is told to write.
# ----------------------------------------------------------------------


def test_instruction_names_the_premises_section(premise_job):
    from prism_service.services.arc_governance import load_rubrics

    section = load_rubrics()["premise_grounded"]["claims_section"]
    assert section == "Premises", (
        "the rubric's claims_section moved; this suite pins the name the "
        f"driver is told to write: {section!r}")
    text = _instructions(premise_job)
    assert section in text, (
        "the review_previous_notes instruction never names the "
        f"{section!r} section the rubric scores: {text!r}")
    assert f"## {section}" in text, (
        "the instruction names the section but not as the markdown HEADING "
        "score_premise_grounded looks for (arc_governance.py:309-311), so a "
        f"driver writes prose and is refused: {text!r}")


# ----------------------------------------------------------------------
# R2 — the ACCEPTED CITATION FORMS, one assertion per form so a partial
# rewrite fails loudly. These are exactly the three positive regexes at
# arc_governance.py:267-275 (_CLAIM_FILELINE_RE / _CLAIM_RUNID_RE /
# _CLAIM_OUTPUT_RE); the line-number clause is the citation-theatre trap
# the scorer documents at arc_governance.py:264-266.
# ----------------------------------------------------------------------


def test_instruction_names_the_file_line_citation_form(premise_job):
    low = _instructions(premise_job).lower()
    assert "file:line" in low, (
        "the instruction never tells the driver a file:line citation is "
        f"accepted grounding: {low!r}")
    assert "line number" in low, (
        "the instruction accepts file:line but never says the LINE NUMBER "
        "is required — a bare path is citation theatre and is refused "
        f"(arc_governance.py:264-267): {low!r}")


def test_instruction_names_the_run_or_commit_id_citation_form(premise_job):
    low = _instructions(premise_job).lower()
    missing = [tok for tok in ("run", "pr", "commit", "issue")
               if not re.search(rf"\b{tok}\b", low)]
    assert not missing, (
        "the instruction never names the run/PR/commit/issue id form "
        f"_CLAIM_RUNID_RE accepts (arc_governance.py:268-269); missing "
        f"{missing}: {low!r}")


def test_instruction_names_the_command_output_citation_form(premise_job):
    low = _instructions(premise_job).lower()
    assert "output" in low and "backtick" in low, (
        "the instruction never names backtick command output as grounding "
        f"(_CLAIM_OUTPUT_RE, arc_governance.py:270-275): {low!r}")


# ----------------------------------------------------------------------
# R3 — the ESCAPE MARKERS. Without them a driver believes an uncited claim
# is unreportable and either fabricates a citation or stalls; _CLAIM_
# MARKER_RE (arc_governance.py:276) accepts the words themselves.
# ----------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["UNVERIFIED", "REFUTED"])
def test_instruction_names_the_escape_marker(premise_job, marker):
    text = _instructions(premise_job)
    assert marker in text, (
        f"the instruction never offers the explicit {marker} marker as an "
        "alternative to a citation, so an honest but uncited claim looks "
        f"unreportable (arc_governance.py:276): {text!r}")


def test_the_named_markers_are_the_ones_the_scorer_accepts(premise_job):
    """Anti-drift: whatever the instruction promises must actually ground a
    claim when fed to the REAL scorer."""
    from prism_service.services.arc_governance import (
        load_rubrics, score_premise_grounded)

    rubric = load_rubrics()["premise_grounded"]
    for marker in ("UNVERIFIED", "REFUTED"):
        notes = f"## Premises\n- no evidence found yet - {marker}\n"
        res = score_premise_grounded({"notes_md": notes}, rubric)
        assert res["ok"] is True, (marker, res)


# ----------------------------------------------------------------------
# R4 — the CONSEQUENCE. An instruction that lists a convention without
# saying it is enforced reads as advice; the step genuinely does not
# advance (arc_governance.py:312-333 via ConductorService.advance_task).
# ----------------------------------------------------------------------


def test_instruction_states_the_report_is_refused_when_ungrounded(premise_job):
    low = _instructions(premise_job).lower()
    assert "refus" in low, (
        "the instruction never says an ungrounded report is REFUSED, so the "
        f"convention reads as optional advice: {low!r}")
    assert "advance" in low, (
        "the instruction never says the step will not advance — the driver "
        f"cannot tell a refusal from a hint: {low!r}")
    for case in ("missing", "empty", "ungrounded"):
        assert case in low, (
            f"the instruction never names the {case!r} refusal case the "
            f"scorer raises (arc_governance.py:312-333): {low!r}")


# ----------------------------------------------------------------------
# R5 — TRUER, NOT LONGER (the task's recorded misfire #3). The whole point
# is a driver who READS it; a paragraph of rubric spec is the same failure
# in the other direction.
# ----------------------------------------------------------------------

_MAX_SENTENCES = 2
_MAX_CHARS = 600


def test_instruction_stays_short_enough_to_be_read(premise_job):
    base = _base(premise_job)
    assert base and base != _instructions(premise_job), (
        "the doctrine splice marker moved; _base() is no longer isolating "
        "the step's own instruction (conductor_flow.py:65-70)")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", base.strip()) if s]
    assert len(sentences) <= _MAX_SENTENCES, (
        f"the instruction runs to {len(sentences)} sentences; it must stay "
        f"one or two so a driver actually reads it: {sentences}")
    assert len(base) <= _MAX_CHARS, (
        f"the instruction is {len(base)} chars against a {_MAX_CHARS} cap")


# ----------------------------------------------------------------------
# R6 — it must land where a DRIVER receives it. A docstring, a comment or
# a models/workflow.py table entry reaches nobody: the rendered base text
# must BE the _GUIDE entry _job() reads at conductor_flow.py:57-58.
# ----------------------------------------------------------------------


def test_the_rendered_instruction_is_the_guide_entry_itself(premise_job):
    from prism_service.api import conductor_flow as cf

    assert _base(premise_job) == cf._GUIDE["review_previous_notes"], (
        "the text a driver receives is not the _GUIDE entry "
        "(conductor_flow.py:35), so the requirement was written somewhere "
        "the job payload never renders")


# ----------------------------------------------------------------------
# R7 — END TO END over the wire a driver actually reaches. conductor_work
# is a thin wrapper over this module (mcp/tools.py:4441 imports it,
# :4476/:4506 call flow_start); GET /api/conductor/flow/next serves the
# identical job payload from the mounted production router.
# ----------------------------------------------------------------------


def _flow_next_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def test_flow_next_is_mounted_on_the_production_api_router():
    client = _flow_next_client()
    paths = set(client.app.openapi()["paths"])
    assert "/api/conductor/flow/next" in paths, (
        "the job read surface is not mounted on the production api_router "
        f"(sample: {sorted(p for p in paths if 'conductor' in p)[:6]})")


def test_the_contract_reaches_a_driver_over_the_http_wire(premise_job):
    resp = _flow_next_client().get(
        "/api/conductor/flow/next",
        params={"task_id": premise_job["task_id"],
                "project": premise_job["project"]})
    assert resp.status_code == 200, resp.text
    job = resp.json()["job"]
    assert job["step"] == "review_previous_notes", job
    text = job["instructions"]
    low = text.lower()
    for needle in ("## Premises", "UNVERIFIED", "REFUTED"):
        assert needle in text, (needle, text)
    for needle in ("file:line", "line number", "backtick", "refus", "advance"):
        assert needle in low, (needle, text)


# ----------------------------------------------------------------------
# R8 — THE GATE IS UNCHANGED (misfire #1: "make the instruction true" must
# never become "make the rubric lenient"). These are regression guards and
# are expected GREEN before and after; a describe-don't-loosen slice that
# reds any of them has loosened the judge instead of documenting it.
# ----------------------------------------------------------------------

_COMPLIANT = ("## Premises\n- the guide entry is a one-liner today - "
              "prism_service/api/conductor_flow.py:35\n")
_THEATRE = ("## Premises\n- the guide entry is a one-liner today - "
            "prism_service/api/conductor_flow.py\n")


@pytest.mark.parametrize("notes,expected", [
    (_COMPLIANT, True),
    (_THEATRE, False),
    ("## Premises\n\nnothing recorded\n", False),
    ("# Review\n\nprose only, no heading\n", False),
    ("", False),
])
def test_premise_grounded_verdicts_are_unchanged(notes, expected):
    from prism_service.services.arc_governance import (
        load_rubrics, score_premise_grounded)

    res = score_premise_grounded(
        {"notes_md": notes}, load_rubrics()["premise_grounded"])
    assert res["ok"] is expected, res


# ----------------------------------------------------------------------
# R9 — the slice must not edit its own judge (control_plane.py:43-50). A
# fix site inside POLICY_FILES fires the candidate-controls-judge tooth and
# fails this task's own gates, so it is pinned at authoring time.
# ----------------------------------------------------------------------

SLICE_FILES = (
    "services/prism-service/prism_service/api/conductor_flow.py",
    "services/prism-service/prism_service/models/workflow.py",
    "services/prism-service/prism_service/__version__.py",
    "services/prism-service/tests/unit/"
    "test_premise_step_states_its_contract.py",
)


def test_no_file_in_this_slice_is_a_gate_policy_file():
    from prism_service.services.control_plane import POLICY_FILES

    overlap = sorted(set(SLICE_FILES) & set(POLICY_FILES))
    assert not overlap, (
        "this slice edits its own judge; move the fix out of POLICY_FILES "
        f"or the candidate-controls-judge tooth blocks the gate: {overlap}")


# ----------------------------------------------------------------------
# R12 — a user-visible change ships a patch bump (the SPA footer is how the
# owner confirms the new build is live).
# ----------------------------------------------------------------------

_VERSION_BEFORE = (7, 10, 5)


def test_prism_version_is_patch_bumped_for_this_change():
    from prism_service.__version__ import PRISM_VERSION

    parts = tuple(int(p) for p in PRISM_VERSION.split(".")[:3])
    assert parts > _VERSION_BEFORE, (
        f"PRISM_VERSION is still {PRISM_VERSION}: a driver-visible "
        "instruction change must patch-bump so the running daemon can be "
        "identified")
