"""A plan_gate FORM failure rewinds instead of escalating to a human.

Task a65c66e5, 01:43Z round: a machine rewind to verify_plan produced a
plan_doc with 3 AC(s) and ZERO `oracle:` lines, plus a plan_diagram with
fewer than two edges. plan_gate then parked with gate_reason "escalating
to you - design-packet certainty 0.00 is below the 0.90 threshold ...".
The owner's rule: no gate parks for a person when the machine can act --
a FORM failure is the planner's to fix.

This pins the fixture against the live shape (copied from the round's own
numbers: 3 ACs, 0 oracle lines, an undersized diagram) at three levels:
  1. plan_gate_checks.form_complete names the exact missing items;
  2. the new tooth is registered as CHECKS[-1] / in LABELS, so
     plan_gate_checks.refusal() actually reports it;
  3. gate_adjudicator's `_hold` short-circuit (the block this test cannot
     reach without a live DB) is documented, not re-tested here -- see
     test_adjudicator_minds_the_root_plan_gate.py for that seat's own
     coverage; this file is the deterministic tooth's own contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import plan_gate_checks as pgc  # noqa: E402

# The live a65c66e5 shape: 3 AC(s), none carrying an `oracle:` line.
_A65C66E5_PLAN_DOC = (
    "## Acceptance Criteria\n"
    "- AC-1: the canvas shows the fixed pill state\n"
    "- AC-2: the pill never reads STALLED while a step agent is writing\n"
    "- AC-3: a re-render after the fix keeps the board live\n"
)
_A65C66E5_DIAGRAM_TOO_SMALL = "flowchart TD\n  A[Start]\n"


def test_form_complete_names_every_ac_missing_an_oracle_line():
    reason = pgc.form_complete(_A65C66E5_PLAN_DOC, _A65C66E5_DIAGRAM_TOO_SMALL)
    assert "3 of 3 AC(s) carry no `oracle:` line" in reason, reason
    assert "AC-1" in reason and "AC-2" in reason and "AC-3" in reason


def test_form_complete_flags_a_diagram_with_fewer_than_two_edges():
    reason = pgc.form_complete(_A65C66E5_PLAN_DOC, _A65C66E5_DIAGRAM_TOO_SMALL)
    assert "fewer than two edges" in reason, reason


def test_form_complete_passes_a_plan_with_oracle_lines_and_a_real_diagram():
    plan = (
        "## Acceptance Criteria\n"
        "- AC-1: the pill never reads STALLED while active\n"
        "  oracle: pytest tests/unit/test_pill_state.py::test_active_is_not_stalled\n"
    )
    diagram = "flowchart TD\n  A[Start] --> B[Check]\n  B --> C[Render]\n"
    assert pgc.form_complete(plan, diagram) == ""


def test_form_complete_is_silent_with_no_ac_entries_at_all():
    # arc_governance's own rubric teeth own the "no ACs" shape; this tooth
    # only judges the ACs that exist.
    assert pgc.form_complete("no acceptance criteria section here", "") == ""


def test_form_complete_is_registered_in_checks_and_labels():
    assert "form_complete" in pgc.CHECKS
    assert "form_complete" in pgc.LABELS


def test_refusal_reports_the_a65c66e5_form_defect_end_to_end():
    """The full run_all()/refusal() surface plan_rewind.maybe_rewind and
    gate_adjudicator's `_hold` short-circuit both read -- confirms the new
    tooth actually reaches the aggregate string, not just its own
    function."""

    class _Task:
        id = "a65c66e5-b8a7-44b4-a223-f1342cfaaa14"
        plan_doc = _A65C66E5_PLAN_DOC
        plan_diagram = _A65C66E5_DIAGRAM_TOO_SMALL
        stop_if: list = []
        verify: list = []

    reason = pgc.refusal(_Task(), "prism", use_cache=False)
    assert "no `oracle:` line" in reason, reason
    assert "fewer than two edges" in reason, reason
