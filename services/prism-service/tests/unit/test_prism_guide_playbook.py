"""prism_guide must teach an installed agent HOW to ORCHESTRATE PRISM.

A self-documenting guide that only LISTS tools doesn't teach the calling
agent the working pattern: epics-as-root-tasks decomposed into demonstrable
subtasks, driven through the conductor SDLC gates, with subagent fan-out and
DISTINCT-actor (no self-override) gate verification, the WHY written back to
memory at green_gate, and the implement/prototype skills doing the drive.

The AC tokens (epic / story_gate / self-override / ...) all appear elsewhere
in the guide blob (the version-notes changelog mentions them), so each AC is
asserted INSIDE the dedicated "Working tasks the PRISM way" playbook section,
not against the whole guide.

Conductor task: b9ddda5e.
"""

from __future__ import annotations

import asyncio
import os

os.environ["PRISM_MCP_AUGMENT_NUDGES"] = "false"

_TITLE = "working tasks the prism way"


def _call(tool_name, arguments=None, project_id="prism"):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool_name, arguments or {}, project_id=project_id))


def _guide_text():
    result = _call("prism_guide", {})
    assert len(result) >= 1
    # Collapse whitespace so a line-wrapped phrase is still matched.
    return " ".join(result[0].text.split()).lower()


def _playbook_section():
    """The "Working tasks the PRISM way" section text, isolated from the rest
    of the guide (so version-notes mentions of the same tokens don't count)."""
    g = _guide_text()
    assert _TITLE in g, "guide lacks the 'Working tasks the PRISM way' playbook"
    return g[g.index(_TITLE):]


# ----------------------------------------------------------------------
# AC-1 — epics are ROOT tasks decomposed into subtasks via parent_id, the
# hierarchy tracked live on the conductor.
# ----------------------------------------------------------------------


def test_playbook_teaches_epic_subtask_hierarchy_on_conductor():
    s = _playbook_section()
    assert "epic" in s
    assert "subtask" in s or "sub-task" in s
    assert "parent_id" in s
    assert "root" in s
    assert "conductor" in s


# ----------------------------------------------------------------------
# AC-2 — the conductor SDLC gate flow is named.
# ----------------------------------------------------------------------


def test_playbook_teaches_the_gate_flow():
    s = _playbook_section()
    assert "story_gate" in s
    assert "plan_gate" in s
    assert "red" in s
    assert "green_gate" in s


# ----------------------------------------------------------------------
# AC-3 — subagent fan-out AND distinct-actor (no self-override) verification.
# ----------------------------------------------------------------------


def test_playbook_teaches_fanout_and_distinct_actor_verification():
    s = _playbook_section()
    assert "subagent" in s
    assert "fan out" in s or "fan-out" in s or "parallel" in s
    assert "distinct" in s or "independent" in s
    assert "self-override" in s or "no self override" in s


# ----------------------------------------------------------------------
# AC-4 — write the decision/WHY back to memory at green_gate.
# ----------------------------------------------------------------------


def test_playbook_teaches_writing_decision_to_memory_at_green_gate():
    s = _playbook_section()
    assert "memory_store" in s
    assert "decision" in s
    assert "green_gate" in s
    assert "rationale" in s or "why" in s


# ----------------------------------------------------------------------
# AC-5 — references the implement (and prototype) skill/workflow.
# ----------------------------------------------------------------------


def test_playbook_references_implement_and_prototype_skills():
    s = _playbook_section()
    assert "implement" in s
    assert "prototype" in s


# ----------------------------------------------------------------------
# AC-6 (v6.7.7) — the playbook TEACHES proof_type-driven gates: declare
# proof_type so red/green check the right oracle, with the per-type shapes
# and the ui-tag deferral spelled out (not just the function signature).
# ----------------------------------------------------------------------


def test_playbook_teaches_proof_type_driven_gates():
    s = _playbook_section()
    assert "proof_type" in s
    # the per-type oracle shapes are named
    assert "metric" in s
    assert "artifact" in s
    assert "demo" in s
    # the TDD default is called out
    assert "default" in s and "tdd" in s
    # the ui-tag no longer silently forces a screenshot
    assert "ui" in s and "screenshot" in s


# ----------------------------------------------------------------------
# AC-7 (v6.7.7) — author to the rubric BEFORE the gate: conductor_advance
# into draft_story/verify_plan returns result['rubric'].
# ----------------------------------------------------------------------


def test_playbook_teaches_rubric_on_advance():
    s = _playbook_section()
    assert "rubric" in s
    assert "draft_story" in s or "verify_plan" in s
    assert "oracle:" in s or "ac-<n>" in s


# ----------------------------------------------------------------------
# AC-8 (v6.7.7) — keep conductor responses LEAN: fields projection on
# conductor_advance/conductor_gate/task_list + parent_id epic scope.
# ----------------------------------------------------------------------


def test_playbook_teaches_lean_responses():
    s = _playbook_section()
    assert "lean" in s
    assert "fields" in s
    assert "from_step" in s and "to_step" in s and "gate_state" in s
    assert "parent_id" in s


# ----------------------------------------------------------------------
# AC-9 (v6.7.7) — prism_onboard returns a best_practices block so a fresh
# agent learns the gate + lean-response patterns up front (not just tools).
# ----------------------------------------------------------------------


def test_onboard_returns_best_practices_block():
    import json

    result = _call("prism_onboard", {})
    payload = json.loads(result[0].text)
    bp = payload.get("best_practices")
    assert isinstance(bp, list) and bp, "prism_onboard lacks a best_practices list"
    blob = " ".join(bp).lower()
    assert "proof_type" in blob
    assert "rubric" in blob
    assert "fields" in blob and "parent_id" in blob


# ----------------------------------------------------------------------
# AC-10 (v6.7.8) — the playbook teaches SAFE MAX FAN-OUT: the two safety
# primitives (disjoint allowed_files + per-child proof_type) and lean
# cohort tracking that lets an epic fan out wide without context blowing.
# ----------------------------------------------------------------------


def test_playbook_teaches_safe_fanout_method():
    s = _playbook_section()
    # the collision boundary
    assert "allowed_files" in s and "disjoint" in s
    # heterogeneous slices each gated on their own proof_type
    assert "proof_type" in s
    # lean cohort tracking is what lifts the fan-out ceiling
    assert "parent_id" in s and "fields" in s
    # explicit safe-fan-out framing
    assert "fan-out" in s or "fan out" in s
