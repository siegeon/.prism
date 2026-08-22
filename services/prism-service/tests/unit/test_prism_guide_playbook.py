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


# ----------------------------------------------------------------------
# CLAUDE.md points agents at `prism_guide(section="conductor")` for "THE
# CANONICAL DOCTRINE" -- that section did not actually exist (only a
# generic "workflow" daily-loop section did), a real doc/code gap closed
# here. Owner, live: "I want you to bake that into the core principles of
# how we build our conductor here" (the governed-agent trust-pipeline
# framing) on top of the pre-existing loop/gate doctrine CLAUDE.md already
# described but the code never actually served.
# ----------------------------------------------------------------------

def _conductor_section():
    from prism_service.mcp.tools import _GUIDE_SECTIONS
    assert "conductor" in _GUIDE_SECTIONS, (
        "prism_guide(section='conductor') must resolve to a real section -- "
        "CLAUDE.md documents this as THE canonical doctrine location")
    return _GUIDE_SECTIONS["conductor"].lower()


def test_conductor_section_is_directly_addressable_and_in_the_full_guide():
    from prism_service.mcp.tools import _GUIDE_SECTIONS

    direct = _call("prism_guide", {"section": "conductor"})[0].text
    assert direct == _GUIDE_SECTIONS["conductor"], (
        "prism_guide(section='conductor') must return that section verbatim")
    full = _guide_text()
    assert "core principles" in full, (
        "the conductor section must be included in the unscoped (full) guide")


def test_conductor_doctrine_names_the_six_trust_pipeline_stages():
    s = _conductor_section()
    for stage in ("proposal", "structure", "meaning", "consistency",
                  "authority", "effect"):
        assert stage in s, f"missing trust-pipeline stage: {stage}"


def test_conductor_doctrine_teaches_the_two_human_gates_and_why_readiness_isnt_the_verdict():
    s = _conductor_section()
    assert "plan_gate" in s and "green_gate" in s
    assert "red_gate" in s and "never" in s  # red_gate never routed to a human
    assert "readiness" in s and "gate_state" in s
    assert "verdict" in s


def test_conductor_doctrine_teaches_override_cannot_skip_the_oracle():
    s = _conductor_section()
    assert "override" in s
    assert "oracle" in s
    idx = s.index("override")
    window = s[max(0, idx - 300):idx + 300]
    assert "oracle" in window, (
        "the override clause must sit near the oracle it cannot skip, not "
        "just mention both words somewhere unrelated in the section")


def test_conductor_doctrine_teaches_the_report_audit_rule():
    s = _conductor_section()
    assert "report audit" in s
    assert "re-measure" in s or "remeasure" in s
    assert "likely_misfire" in s


def test_conductor_doctrine_teaches_done_means_shipped():
    s = _conductor_section()
    assert "ship_worker" in s
    assert "stranded" in s or "unmerged" in s or "unpushed" in s


def test_conductor_doctrine_ties_subtask_decomposition_to_the_depth_tree_principle():
    """Owner, live: "the whole point of TASKS and Sub-TASKS is the same
    point as the dont be lazy skill to subdivide until its small enough to
    be reasonably done." """
    s = _conductor_section()
    assert "parent_id" in s
    assert "depth tree" in s
    assert "subdivide" in s
    assert "allowed_files" in s


def test_conductor_doctrine_distinguishes_the_two_delivery_paths():
    """Owner, live: 'if i tell you to work the ticket in prism as a user
    would you need to move it through the system, if you need to fix the
    system from without you can direct work it without using prism (still
    use the brain) just not the tasks.' A formal gated task for
    direct-commit work can never earn a gate_state, which reads as
    unfinished when it isn't -- the doctrine must say so explicitly."""
    s = _conductor_section()
    assert "conductor_work" in s
    assert "gate_state" in s
    assert "memory_store" in s
    assert "direct commit" in s
