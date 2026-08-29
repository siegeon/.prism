"""Plan rubric refuses a plan written for another task (task 777fbec2).

On 2026-08-28 two root plans passed score_plan_coverage with the wrong
subject: f1906073's plan opened with "This plan covers task a834b2ce's
story", and 9f60a849's plan described itself as a replacement for a plan
copied from e5b75a98. The scorer (services/arc_governance.py) scores only
form. This suite pins a fourth tooth, ``plan_subject``:

  * the FIRST non-empty line of plan_doc must not name a task id that is
    not evidence["task_id"] (prefix compare, 8-hex or full uuid);
  * the first line must not describe the plan as a replacement for a
    contaminated plan;
  * body lines are never scanned (likely_misfire: a sibling citation in
    the body must NOT trip the rule);
  * the tooth is yaml data (governance_rubrics.yaml plan_coverage:
    plan_subject) and the gate seat passes the task id.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

OWN_ID = "f1906073-1111-4222-8333-444455556666"
FOREIGN_ID = "a834b2ce"

STORY = """# Story: subject sample

## Summary
Sample story for the plan_subject tooth.

## Requirements
- FR-1: the plan is about the task it sits on

## Acceptance Criteria
- AC-1: a foreign subject line is refused — oracle: pytest \
tests/unit/test_plan_rubric_refuses_foreign_subject.py
"""

PRINCIPLES = [{
    "id": "ARC-1", "kind": "layer_rule",
    "from": "domain", "must_not_depend_on": "infrastructure",
}]

DIAGRAM = "flowchart TD\n  A --> B"

FOREIGN_PLAN = ("This plan covers task a834b2ce's story.\n\n"
                "## Plan\n- step 1 covers AC-1\n")
CONTAMINATED_PLAN = (
    "The plan_doc currently on file for this task is contaminated — it is "
    "a verbatim copy of a different epic's plan.\n\n"
    "## Plan\n- step 1 covers AC-1\n")
OWN_SUBJECT_SIBLING_BODY_PLAN = (
    f"This plan covers task {OWN_ID[:8]}.\n\n"
    "## Plan\n- step 1 covers AC-1 and depends on task a834b2ce\n")


def _gov():
    from prism_service.services import arc_governance
    return arc_governance


def _rubric():
    return _gov().load_rubrics()["plan_coverage"]


def _score(plan_doc: str, rubric=None, task_id=OWN_ID):
    ev = {"story_md": STORY, "plan_doc": plan_doc, "plan_diagram": DIAGRAM}
    if task_id:
        ev["task_id"] = task_id
    return _gov().score_plan_coverage(ev, rubric or _rubric(), PRINCIPLES)


def test_foreign_subject_line_is_refused():
    """AC-1: subject line names a task id that is not the task under
    review -> ok=false, reason names the foreign id."""
    res = _score(FOREIGN_PLAN)
    assert res["ok"] is False, res
    assert FOREIGN_ID in res["reason"], res


def test_contaminated_replacement_is_refused():
    """AC-2: a plan that calls itself a replacement for a contaminated
    plan -> ok=false, reason says contaminated."""
    res = _score(CONTAMINATED_PLAN)
    assert res["ok"] is False, res
    assert "contaminated" in res["reason"], res


def test_sibling_in_body_passes():
    """AC-3 (likely_misfire guard): own id on the subject line and a
    sibling id cited in the BODY -> ok=true."""
    res = _score(OWN_SUBJECT_SIBLING_BODY_PLAN)
    assert res["ok"] is True, res


def test_no_task_id_skips_subject_compare():
    """AC-4: without evidence["task_id"] the foreign-subject compare is
    skipped (callers in api/workflows.py pass no task id)."""
    res = _score(FOREIGN_PLAN, task_id="")
    assert "plan_subject: subject line" not in res.get("reason", ""), res


def test_rubric_yaml_carries_plan_subject():
    """AC-5: the tooth is data. governance_rubrics.yaml carries
    plan_coverage.plan_subject, and enabled=false disables it."""
    rub = _rubric()
    assert "plan_subject" in rub, sorted(rub)
    off = dict(rub, plan_subject={"enabled": False})
    res = _score(FOREIGN_PLAN, rubric=off)
    assert res["ok"] is True, res


def test_gate_seat_passes_task_id(monkeypatch):
    """AC-6: ConductorService._verify_rubric_gate hands the task's own id
    to the scorer as evidence["task_id"]."""
    from prism_service.services.conductor_service import ConductorService
    gov = _gov()
    seen: dict = {}

    def _capture(evidence, rubric, principles):
        seen.update(evidence)
        return {"ok": True, "reason": "captured"}

    monkeypatch.setattr(gov, "score_plan_coverage", _capture)

    class _Task:
        id = OWN_ID
        plan_doc = FOREIGN_PLAN
        plan_diagram = DIAGRAM
        premise_notes = ""
        oracle = ""

    svc = ConductorService.__new__(ConductorService)
    svc._memory_svc = None
    svc._verify_rubric_gate(_Task(), "plan_coverage")
    assert seen.get("task_id") == OWN_ID, seen
