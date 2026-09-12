"""plan_gate's new tooth: plan_diagram must be STRUCTURALLY valid mermaid,
not merely open with a known diagram-type keyword.

arc_governance.mermaid_parses (the e2 tooth in score_plan_coverage) only
checks that the first non-empty line names a diagram type -- it never
checks that the body is well-formed. Task a65c66e5 reached plan_gate
carrying a plan_diagram whose last node's label bracket was never closed
("E[Validate change with gate tests" with no trailing "]"), so PlanView
showed the owner "Diagram failed to render: Parse error on line 6 ... got
'1'" on the very screen plan_gate exists to let them review. The rubric
tooth said ok because "flowchart TD" is a known keyword; nothing else
looked at the body.

This tooth (services/plan_gate_checks.py: plan_diagram_parses) runs a
real structural check -- balanced [], (), {} and balanced quotes -- and
refuses when the diagram cannot possibly render, naming the line.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import plan_gate_checks as pgc  # noqa: E402


# The REAL plan_diagram shipped on task a65c66e5 (fetched live from
# http://localhost:7780/api/tasks/a65c66e5-...): four closed nodes, a
# fifth whose "[" is never closed and no further edges after it.
_A65C66E5_DIAGRAM = (
    "flowchart TD\n"
    "    A[Identify flagging logic] --> B[Modify to check for existing task IDs]\n"
    "    B --> C[Add exemptions for specific keywords]\n"
    "    C --> D[Reference all AC-<n> IDs from the story]\n"
    "    D --> E[Validate change with gate tests"
)

_CORRECTED_DIAGRAM = (
    "flowchart TD\n"
    "    A[Identify flagging logic] --> B[Modify to check for existing task IDs]\n"
    "    B --> C[Add exemptions for specific keywords]\n"
    "    C --> D[Reference all AC ids from the story]\n"
    "    D --> E[Validate change with gate tests]\n"
)


def test_the_real_a65c66e5_diagram_is_refused():
    """The literal live defect: an unclosed node label on line 5."""
    reason = pgc.plan_diagram_parses(_A65C66E5_DIAGRAM)
    assert reason, "an unclosed '[' must refuse, not silently pass"
    assert "line 5" in reason, reason
    assert "[" in reason, reason


def test_arc_governances_own_cheap_check_still_says_this_parses():
    """Pin the actual gap: the FIRST-LINE-only tooth calls this diagram
    fine, which is exactly why plan_diagram_parses had to be added."""
    from prism_service.services import arc_governance as gov
    assert gov.mermaid_parses(_A65C66E5_DIAGRAM) is True


def test_a_corrected_diagram_with_the_bracket_closed_passes():
    assert pgc.plan_diagram_parses(_CORRECTED_DIAGRAM) == ""


def test_empty_plan_diagram_degrades_to_a_pass():
    """arc_governance's own require_plan_diagram tooth owns "missing" --
    this tooth must never double-refuse an empty diagram."""
    assert pgc.plan_diagram_parses("") == ""
    assert pgc.plan_diagram_parses(None) == ""


def test_an_unmatched_closing_bracket_is_refused():
    reason = pgc.plan_diagram_parses("flowchart TD\n    A --> B]\n")
    assert reason, reason
    assert "line 2" in reason, reason


def test_a_mismatched_bracket_type_is_refused():
    reason = pgc.plan_diagram_parses("flowchart TD\n    A[Node (oops]\n")
    assert reason, reason
    assert "line 2" in reason, reason


def test_an_unterminated_quote_is_refused():
    reason = pgc.plan_diagram_parses('flowchart TD\n    A["Node label\n')
    assert reason, reason
    assert "line 2" in reason, reason


def test_a_normal_well_formed_flowchart_passes():
    diagram = ("flowchart TD\n"
               "  api --> services\n"
               "  services --> models\n"
               "  services -.-> brain[(brain.db)]\n")
    assert pgc.plan_diagram_parses(diagram) == ""


def test_a_normal_well_formed_sequence_diagram_passes():
    diagram = ("sequenceDiagram\n"
               "  participant U as User\n"
               "  participant S as prism-service\n"
               "  U->>S: POST /api/conductor/gate\n"
               "  S-->>U: {ok, gate_state}\n")
    assert pgc.plan_diagram_parses(diagram) == ""


def test_the_tooth_is_registered_in_checks_and_labels():
    assert "plan_diagram_parses" in pgc.CHECKS
    assert "plan_diagram_parses" in pgc.LABELS


def test_run_all_includes_the_new_tooth_by_id():
    from prism_service.models.task import Task
    task = Task(id="probe", title="t", plan_diagram=_A65C66E5_DIAGRAM)
    entries = {e["id"]: e for e in pgc.run_all(task, "pgc-" + uuid.uuid4().hex[:8],
                                               use_cache=False)}
    assert "plan_diagram_parses" in entries
    assert entries["plan_diagram_parses"]["ok"] is False
    assert "line 5" in entries["plan_diagram_parses"]["reason"]


# ----------------------------------------------------------------------
# The seat: a refusal WITHHOLDS plan_gate, it does not merely report it.
# Same pattern as test_plan_gate_deterministic_checks.py's seat tests --
# every tooth added to CHECKS must be asked for again at the seat that
# actually decides (autoclear + adjudicator), not just at the card.
# ----------------------------------------------------------------------
def _child_task_at_plan_gate(*, plan_diagram, plan_doc):
    from prism_service.project_context import get_project
    project = "pgc-diagram-" + uuid.uuid4().hex[:8]
    svc = get_project(project).conductor_svc
    parent = svc._task_svc.create(title="plan gate teeth parent")
    task = svc._task_svc.create(title="plan gate diagram probe")
    svc._task_svc.update(task.id, parent_id=parent.id, plan_doc=plan_doc,
                         plan_diagram=plan_diagram,
                         workflow_step="plan_gate", gate_state="pending")
    svc._verify_gate = lambda *a, **k: {"verified": True,
                                        "reason": "rubric stubbed green"}
    pgc.clear_cache()
    return project, svc, task.id


_CLEAN_PLAN = ("## Acceptance Criteria\n"
              "- AC-1 - the diagram renders. RED at HEAD.\n"
              "  - oracle: pytest tests/unit/test_plan_diagram_parses.py\n")


def test_the_autoclear_seat_withholds_plan_gate_on_an_unparseable_diagram():
    from prism_service.api import conductor_flow as cf
    project, svc, task_id = _child_task_at_plan_gate(
        plan_diagram=_A65C66E5_DIAGRAM, plan_doc=_CLEAN_PLAN)
    res = cf._autoclear_machine_gate(svc, task_id)
    t = svc._task_svc.get(task_id)
    assert res is None, res
    assert t.gate_state == "pending", t.gate_state
    assert "not valid mermaid" in (t.gate_reason or ""), t.gate_reason


def test_the_adjudicator_seat_reports_the_same_refusal():
    from prism_service.services import gate_adjudicator as ga
    project, svc, task_id = _child_task_at_plan_gate(
        plan_diagram=_A65C66E5_DIAGRAM, plan_doc=_CLEAN_PLAN)
    svc._validation_for_gate = lambda *a, **k: "plan_coverage"
    svc._verify_rubric_gate = lambda *a, **k: {"verified": True, "reason": ""}
    task = svc._task_svc.get(task_id)
    reason = ga._pending_decline_reason(svc, task, "plan_gate", project)
    assert "not valid mermaid" in reason, reason
