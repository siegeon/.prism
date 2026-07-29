"""RED scaffold — premise_grounded gate on review_previous_notes
(task 3a63190b / github issue #222).

The conductor gates whether a story is WELL-FORMED (required sections, AC
ids + oracles) but never whether it is TRUE. `review_previous_notes` is the
only WORKFLOW_STEPS entry with `validation: None`
(prism_service/models/workflow.py). This pins issue #222 proposal (a): a
`premise_grounded` rubric that REFUSES the review_previous_notes report when
a load-bearing claim carries neither a citation (file:line, a run/PR/
commit/issue id, or backtick command output) nor an explicit REFUTED/
UNVERIFIED marker — and the refusal NAMES the offending claim.

Recorded task.likely_misfire, both teeth pinned here:
  (1) CITATION THEATRE — a bare path with no line number ('src/foo.py')
      must NOT be accepted as a citation.
  (2) WIRING — a test that only calls score_premise_grounded directly is
      not evidence the gate exists on a real drive: the kind must be
      registered in conductor_service._VERIFIER_RULES and actually consulted
      by ConductorService.advance_task, the SAME chokepoint every real
      conductor_work report passes through. The wiring tests below never
      import arc_governance and never call the scorer directly — they
      drive advance_task end to end.

FAILS today: WORKFLOW_STEPS['review_previous_notes']['validation'] is None,
conductor_service._VERIFIER_RULES has no 'premise_grounded' entry, and
arc_governance has no score_premise_grounded.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


COMPLIANT_NOTES = """# Review notes

## Premises
- The failing CI lane is `unit-tests.yml`, configured identically to \
the integration lane — services/prism-service/.github/workflows/unit-tests.yml:12
- The repo ships no Testcontainers configuration — UNVERIFIED, grep found \
no hits yet, re-check before shipping this claim as fact
"""

# Same claims, but the first citation is a bare path with no line number —
# the citation-theatre misfire this rubric exists to catch.
THEATRE_NOTES = COMPLIANT_NOTES.replace(
    "services/prism-service/.github/workflows/unit-tests.yml:12",
    "services/prism-service/.github/workflows/unit-tests.yml",
)

NO_SECTION_NOTES = "# Review notes\n\nJust some prose, no Premises heading.\n"

EMPTY_SECTION_NOTES = "# Review notes\n\n## Premises\n\nNothing recorded yet.\n"


def _gov():
    from prism_service.services import arc_governance
    return arc_governance


def _rubric():
    return _gov().load_rubrics()["premise_grounded"]


# ── rubric is YAML data ──────────────────────────────────────────────────

def test_premise_grounded_rubric_is_yaml_data():
    rub = _gov().load_rubrics()
    assert "premise_grounded" in rub


# ── pure scorer (AC-2, AC-3, AC-4) ───────────────────────────────────────

def test_compliant_notes_score_ok():
    res = _gov().score_premise_grounded(
        {"notes_md": COMPLIANT_NOTES}, _rubric())
    assert res["ok"] is True, res


def test_bare_path_without_line_number_is_citation_theatre():
    """likely_misfire (1): a path with no line number is NOT a citation."""
    res = _gov().score_premise_grounded(
        {"notes_md": THEATRE_NOTES}, _rubric())
    assert res["ok"] is False, (
        "a bare path with no line number must not satisfy the rubric")


def test_missing_premises_section_fails():
    res = _gov().score_premise_grounded(
        {"notes_md": NO_SECTION_NOTES}, _rubric())
    assert res["ok"] is False
    assert "Premises" in res["reason"], res


def test_empty_premises_section_fails():
    res = _gov().score_premise_grounded(
        {"notes_md": EMPTY_SECTION_NOTES}, _rubric())
    assert res["ok"] is False


def test_empty_notes_fail():
    res = _gov().score_premise_grounded({"notes_md": ""}, _rubric())
    assert res["ok"] is False


def test_refusal_names_the_offending_claim():
    res = _gov().score_premise_grounded(
        {"notes_md": THEATRE_NOTES}, _rubric())
    assert "unit-tests.yml" in res["reason"], (
        "refusal must name the offending claim so the driver can "
        f"self-diagnose: {res}")


def test_refuted_marker_is_accepted_as_grounding():
    notes = "# Review notes\n\n## Premises\n- The CI lanes differ — REFUTED, both jobs use the same runner image\n"
    res = _gov().score_premise_grounded({"notes_md": notes}, _rubric())
    assert res["ok"] is True, res


# ── b3-style wiring: the kind is registered, not just the scorer ─────────

def test_verifier_rules_registers_premise_grounded():
    from prism_service.services.conductor_service import ConductorService
    rule = ConductorService._VERIFIER_RULES.get("premise_grounded")
    assert rule is not None, "premise_grounded missing from _VERIFIER_RULES"
    assert rule.get("rubric") == "premise_grounded", rule


def test_workflow_step_carries_the_validation_kind():
    from prism_service.models.workflow import WORKFLOW_STEPS
    step = next(s for s in WORKFLOW_STEPS
                if s["id"] == "review_previous_notes")
    assert step["validation"] == "premise_grounded", (
        "review_previous_notes still has validation=None (issue #222)")


def test_story_gate_inheritance_is_unaffected():
    """Guard against a regression this rubric could cause: draft_story's
    own validation must still win story_gate's inheritance (issue #222's
    design constraint — premise_grounded cannot ride gate inheritance)."""
    from prism_service.services.conductor_service import ConductorService
    assert ConductorService._validation_for_gate("story_gate") == \
        "story_complete", (
        "story_gate must still inherit story_complete, not "
        "premise_grounded, even though review_previous_notes now carries "
        "a validation kind")


# ── end-to-end wiring through the REAL advance_task chokepoint ──────────
# (the exact path conductor_flow.flow_report calls for every non-gate step
# report) — no arc_governance import here, so a scorer-only test cannot
# fake this evidence.

def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def test_advance_task_refuses_ungrounded_notes_through_the_real_wiring(
        tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="premise gate wiring: bad notes")
    res0 = cond.advance_task(t.id)  # '' -> review_previous_notes
    assert res0["to_step"] == "review_previous_notes", res0

    task_svc.update(t.id, completion_proof=THEATRE_NOTES)
    res1 = cond.advance_task(t.id)
    assert res1["ok"] is False, (
        "advance_task must REFUSE review_previous_notes -> draft_story on "
        f"ungrounded notes; got {res1}")
    assert "unit-tests.yml" in res1.get("reason", ""), res1
    t2 = task_svc.get(t.id)
    assert t2.workflow_step == "review_previous_notes", (
        "a refused report must not advance the workflow step")


def test_advance_task_advances_on_compliant_notes(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="premise gate wiring: compliant notes")
    cond.advance_task(t.id)  # -> review_previous_notes
    task_svc.update(t.id, completion_proof=COMPLIANT_NOTES)
    res = cond.advance_task(t.id)
    assert res["ok"] is True, res
    assert res["to_step"] == "draft_story", res


def test_refusal_reason_is_recorded_on_the_task_for_self_diagnosis(
        tmp_path):
    """The refusal must be readable off the TASK (gate_reason), not just
    the advance_task return value, so a driver polling
    conductor_work/task_list can self-diagnose without re-deriving the
    rubric (the same convention story_gate/plan_gate already use)."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="premise gate wiring: reason on task")
    cond.advance_task(t.id)
    task_svc.update(t.id, completion_proof=THEATRE_NOTES)
    cond.advance_task(t.id)
    t2 = task_svc.get(t.id)
    assert getattr(t2, "gate_reason", ""), (
        "a refused premise_grounded report must record an actionable "
        "gate_reason on the task")
    assert "unit-tests.yml" in t2.gate_reason, t2.gate_reason


def test_advance_task_clears_stale_reason_on_a_later_compliant_report(
        tmp_path):
    """A driver that fixes its notes after a refusal must not still see
    the stale reason once it has advanced past the step."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="premise gate wiring: reason clears")
    cond.advance_task(t.id)
    task_svc.update(t.id, completion_proof=THEATRE_NOTES)
    cond.advance_task(t.id)  # refused, gate_reason set
    task_svc.update(t.id, completion_proof=COMPLIANT_NOTES)
    res = cond.advance_task(t.id)  # now compliant -> advances
    assert res["ok"] is True, res
    t2 = task_svc.get(t.id)
    assert t2.workflow_step == "draft_story"
    assert not (getattr(t2, "gate_reason", "") or ""), (
        "a stale premise_grounded refusal reason must not linger once "
        f"the task has advanced past the step: {t2.gate_reason!r}")
