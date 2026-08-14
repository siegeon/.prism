"""Tests for the decision-packet 'subject' channel (task 31c345b7, walking
skeleton of epic fcf6b70b).

decision_packet.assemble_packet had exactly 5 keys (state/diff_stat/commits/
receipt/screenshots), all sourced from POST-code artifacts that cannot exist
at story_gate/plan_gate. This adds one additive 'subject' channel carrying
the story/plan markdown a gate at those two steps is actually deciding on.

AC-1/AC-2/AC-3 pin the backend channel; AC-4 pins the existing 5 keys stay
shaped exactly as before; AC-5/AC-6 pin the TSX render guard by parsing the
ACTUAL SOURCE of DecisionPacket.tsx (this SPA has no JS test runner, per
CLAUDE.md and test_conductor_page_animated_cleanup_ui.py:4-6) with comment
lines stripped first and the enclosing JSX conditional extracted by brace
depth, never a fixed character window (test_gate_banner_honest_state_ui.py:
51-67 / test_gate_banner_names_the_packet.py:55-77 convention, reused
verbatim below).
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_DECISION_PACKET_TSX = _SRC / "components" / "plan" / "DecisionPacket.tsx"


def _base_task(**overrides):
    fields = dict(workflow_step="story_gate", gate_state="pending",
                  status="in_progress", gate_reason="",
                  plan_doc="", plan_diagram="")
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _packet(task, monkeypatch):
    from prism_service.services import decision_packet, task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for", lambda tid: None)
    monkeypatch.setattr(decision_packet, "_receipt", lambda p, t: None)
    monkeypatch.setattr(decision_packet, "_screenshots", lambda t: [])
    return decision_packet.assemble_packet("prism", "31c345b7", task)


# ---------------------------------------------------------------------------
# AC-1 / AC-2 / AC-3 - backend subject channel
# ---------------------------------------------------------------------------

def test_subject_present_at_story_gate_with_plan_doc(monkeypatch):
    task = _base_task(workflow_step="story_gate",
                       plan_doc="## Acceptance Criteria\n- AC-1: ...")
    pkt = _packet(task, monkeypatch)
    assert pkt["subject"] is not None
    assert pkt["subject"]["kind"] == "story"
    assert "AC-1" in pkt["subject"]["markdown"]


def test_subject_present_at_plan_gate_with_diagram(monkeypatch):
    task = _base_task(workflow_step="plan_gate",
                       plan_doc="# PLAN\nsome plan text",
                       plan_diagram="flowchart TD\n  a --> b")
    pkt = _packet(task, monkeypatch)
    assert pkt["subject"] is not None
    assert pkt["subject"]["kind"] == "plan"
    assert "some plan text" in pkt["subject"]["markdown"]
    assert "flowchart TD" in pkt["subject"]["diagram"]


def test_subject_none_when_plan_doc_and_diagram_both_empty(monkeypatch):
    task = _base_task(workflow_step="story_gate", plan_doc="", plan_diagram="")
    pkt = _packet(task, monkeypatch)
    assert pkt["subject"] is None


# ---------------------------------------------------------------------------
# AC-4 - the existing 5 keys keep their exact shape, additive change only
# ---------------------------------------------------------------------------

def test_existing_keys_unchanged_shape(monkeypatch):
    task = _base_task(workflow_step="green_gate", plan_doc="", plan_diagram="")
    pkt = _packet(task, monkeypatch)
    assert pkt["diff_stat"] == {"files": 0, "insertions": 0,
                                "deletions": 0, "entries": []}
    assert pkt["commits"] == []
    assert pkt["receipt"] is None
    assert pkt["screenshots"] == []
    assert pkt["state"] == "pending"
    # subject is additive and honestly None here (no plan_doc set).
    assert pkt["subject"] is None


# ---------------------------------------------------------------------------
# AC-5 / AC-6 - DecisionPacket.tsx renders the subject Row only at
# story_gate/plan_gate, empty-scoped on pkt.subject (never a literal).
# ---------------------------------------------------------------------------

def _strip_comments(src: str) -> str:
    """Strip // line comments and /* */ block comments so a comment cannot
    satisfy an assertion (verbatim convention, test_gate_banner_names_the_
    packet.py:55-64)."""
    no_block = re.sub(r"/\*[\s\S]*?\*/", "", src)
    no_line = re.sub(r"//[^\n]*", "", no_block)
    return no_line


def _extract_braced_expr(src: str, marker: str) -> str:
    """Return the JSX `{...}` expression starting at the first `{` at/after
    `marker`, matched by BRACE DEPTH, never a fixed character window
    (verbatim convention, test_gate_banner_honest_state_ui.py:51-67)."""
    idx = src.index(marker)
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces scanning forward from marker {marker!r}")


def test_subject_row_guarded_to_story_and_plan_gate_only():
    src = _strip_comments(_DECISION_PACKET_TSX.read_text(encoding="utf-8"))
    expr = _extract_braced_expr(src, '{(step === "story_gate"')

    assert '"story_gate"' in expr
    assert '"plan_gate"' in expr
    # AC-5: the guard must not admit red_gate/green_gate.
    assert '"red_gate"' not in expr
    assert '"green_gate"' not in expr


def test_subject_row_empty_prop_keys_on_pkt_subject():
    src = _strip_comments(_DECISION_PACKET_TSX.read_text(encoding="utf-8"))
    expr = _extract_braced_expr(src, '{(step === "story_gate"')

    # AC-6: empty must be driven by a falsy check on pkt.subject, never a
    # hardcoded literal.
    assert "empty={!pkt.subject}" in expr
    assert "empty={false}" not in expr
    assert "empty={true}" not in expr
