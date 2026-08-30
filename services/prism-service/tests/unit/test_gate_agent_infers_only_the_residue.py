"""The gate's inference seat: rubric first, inference for the unknowns.

Owner, 2026-08-29: "the gate itself is its own state the conductor bot can
hand a task to, and the gate should be working a -p claude instance that is
the adjudicator", and on the layering: "it should be inferred AND rubric.
the idea of every state a bot visits is to make it the MOST deterministic it
can be by codifying things as much as it can, and leaving room for inference
to deal with unknowns."

Before this, task_runner REFUSED gates outright ("NEVER decides a gate ...
if step is None or step['type'] == 'gate'"), so a gate state had no worker
at all -- which is also why its progress bar had nothing real to show.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import gate_agent as ga  # noqa: E402


class _Task:
    def __init__(self, **kw):
        self.title = kw.get("title", "a task")
        self.oracle = kw.get("oracle", "the oracle")
        self.likely_misfire = kw.get("likely_misfire", "the misfire")
        self.plan_doc = kw.get("plan_doc", "")
        self.completion_proof = kw.get("completion_proof", "")
        self.verify = kw.get("verify", [])
        self.proof_type = kw.get("proof_type", "test")


# ---------------------------------------------------------------- verdicts
def test_unreadable_output_is_no_decision_not_an_approval():
    """A seat that fails OPEN rubber-stamps whatever it could not parse."""
    for junk in ("", "   ", "looks good to me!", "{not json", "[]",
                 '{"verdict": "maybe"}', '{"reason": "no verdict key"}'):
        assert ga.parse_verdict(junk) is None, junk


def test_a_verdict_is_read_out_of_surrounding_prose():
    v = ga.parse_verdict(
        'Thinking about it...\n{"verdict":"reject","reason":"only adjacent"}\ndone')
    assert v == {"verdict": "reject", "reason": "only adjacent"}


def test_both_verdict_words_are_accepted():
    assert ga.parse_verdict('{"verdict":"approve","reason":"ok"}')["verdict"] == "approve"
    assert ga.parse_verdict('{"verdict":"REJECT","reason":"no"}')["verdict"] == "reject"


# ---------------------------------------------------------------- posture
def test_the_seat_is_read_only():
    """It may look; it may not touch. A seat that edits the tree it judges
    is the 7.13.171/172 bug, where minting dirtied the very worktree the
    cleanliness tooth then rejected."""
    assert set(ga.REVIEW_TOOLS) == {"Read", "Glob", "Grep"}
    for forbidden in ("Write", "Edit", "Bash", "NotebookEdit"):
        assert forbidden not in ga.REVIEW_TOOLS


def test_the_seat_has_its_own_actor_identity():
    """The no-self-review rule reads `actor`; this seat must never be
    mistaken for the session that produced the work."""
    assert ga.SEAT_ID == "conductor-adjudicator-agent"
    assert ga.SEAT_ID != "prism-task-runner"


def test_it_is_off_unless_the_environment_opts_in(monkeypatch):
    monkeypatch.delenv("PRISM_GATE_AGENT_ENABLED", raising=False)
    assert ga.is_enabled() is False
    monkeypatch.setenv("PRISM_GATE_AGENT_ENABLED", "1")
    assert ga.is_enabled() is True


# ---------------------------------------------------------------- packet
def test_the_packet_carries_the_oracle_and_the_misfire():
    """Judging 'does this evidence demonstrate the oracle, or only look like
    it' is impossible without both."""
    packet = ga.build_packet(
        _Task(oracle="the bar holds at a gate",
              likely_misfire="it holds because nothing polls it"),
        "green_gate")
    assert "the bar holds at a gate" in packet
    assert "it holds because nothing polls it" in packet
    assert "green_gate" in packet


def test_the_prompt_tells_the_seat_the_rubric_already_passed():
    """Otherwise inference re-litigates what code already settled, and an
    approve reads as 'the checks passed' rather than a judgement."""
    packet = ga.build_packet(_Task(), "plan_gate")
    prompt = ga.PROMPT.format(packet=packet)
    low = prompt.lower()
    assert "already passed" in low
    assert "read-only" in low
    assert "reject" in low


# ------------------------------------------------------- ordering + scope
def test_a_demo_green_gate_is_never_taken_from_the_human(monkeypatch, tmp_path):
    """proof_type demo/review is human-only by owner rule eaafdf75 and the
    machine seat abstains BY DESIGN. Inference must not relieve a person of
    a judgement that was deliberately theirs."""
    monkeypatch.setenv("PRISM_GATE_AGENT_ENABLED", "1")
    called = {"n": 0}

    class _Ctx:
        class task_svc:
            @staticmethod
            def get(_):
                return _Task(proof_type="demo")
    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda _p: _Ctx())

    def _invoke(*a, **k):
        called["n"] += 1
        raise AssertionError("inference ran on a human-only gate")

    assert ga.adjudicate("prism", "t1", "green_gate", invoke=_invoke) is None
    assert called["n"] == 0


def test_inference_runs_only_when_the_rubric_states_no_refusal():
    """The sweep must consult the codified half FIRST and stop there when it
    has a reason -- a refusal is already a decision."""
    src = (Path(__file__).resolve().parent.parent.parent
           / "prism_service/services/gate_adjudicator.py").read_text()
    i = src.index("_decline = _pending_decline_reason(")
    j = src.index("gate_agent.adjudicate(", i)
    between = src[i:j]
    assert "if not str(_decline or \"\").strip():" in between, (
        "the inference seat is not gated on the deterministic half being "
        "silent; a codified refusal could be talked away by inference")


# ------------------------------------------------- the flow-state shape
def test_inference_is_a_state_INSIDE_each_gate_not_a_top_level_peer():
    """Owner 2026-08-29, pointing at the rail: "that is not part of a gate
    step, that folder like the steps is hierarchical, and should not be at
    that level since that is not a connected step in the bot's top level
    behavior flow."

    The rule already existed and I broke it. Changelog 7.12.16: "the
    conductor IS its 10-state FSM, and progressive disclosure under it
    should only show what an actual state CALLS (verify_green_state ->
    validation, via linked_workflow_id) ... nesting them there claimed a
    link that doesn't exist."

    Every gate state already links to its OWN behaviour, and those
    behaviours' steps ARE that gate's codified layer. So inference is the
    LAST state inside each of them -- after everything that gate could
    codify -- never a sibling of the flow's real steps.
    """
    import json
    bdir = Path(__file__).resolve().parents[4] / ".prism/behaviors/conductor"

    assert not (bdir / "gate-adjudication.json").exists(), (
        "adjudication is back as a top-level behaviour; no conductor state "
        "calls it, so it claims a link that does not exist")

    for name in ("story-gate-check", "plan-gate-check",
                 "red-gate-status", "green-gate-status"):
        doc = json.loads((bdir / f"{name}.json").read_text(encoding="utf-8"))
        ids = [s["id"] for s in doc["steps"]]
        assert "infer" in ids, f"{name} has no inference state: {ids}"
        assert ids[-1] == "infer", (
            f"{name} runs inference before it has finished codifying: {ids}")


def test_the_gate_behaviours_are_reached_by_drilling_into_a_gate_state():
    """Each gate STATE calls its own behaviour, which is what makes the
    nesting real rather than asserted."""
    src = (Path(__file__).resolve().parent.parent.parent
           / "prism_service/api/workflows.py").read_text(encoding="utf-8")
    for step_id, behaviour in (
            ("story_gate", "story-gate-check"),
            ("plan_gate", "plan-gate-check"),
            ("red_gate", "red-gate-status"),
            ("green_gate", "green-gate-status")):
        assert f'"{behaviour}" if step["id"] == "{step_id}"' in src, (
            f"{step_id} does not link to {behaviour}")
    assert '"gate-adjudication"' not in src, (
        "the top-level peer registration is back")


def test_the_workflow_rail_entries_are_clickable_by_an_agent():
    """Owner 2026-08-29: "CLICK ON IT AS A USER WOULD thats why you have
    remote assist... so you can show stuff."

    The bridge resolves selectors with a plain document.querySelector (CSS
    only, no text matching -- lib/agentBridge.tsx resolveSelector), and these
    rail buttons carried no id, no data attribute and no aria-label. An agent
    could SEE the entry in a screenshot and had no way to click it, so it
    could never demonstrate anything on this page.
    """
    src = (Path(__file__).resolve().parent.parent.parent
           / "prism_service/web/src/pages/WorkflowsPage.tsx").read_text(encoding="utf-8")
    assert src.count("data-workflow-id=") >= 2, (
        "both rail levels (root workflows and nested behaviours) need a "
        "stable selector hook; found "
        f"{src.count('data-workflow-id=')}")
    # the hook must carry the id itself, not a static string
    assert "data-workflow-id={child.id}" in src
    assert "data-workflow-id={workflow.id}" in src


# ------------------------------------------------------------- depth
def test_a_behaviour_step_can_call_a_deeper_behaviour():
    """Owner 2026-08-29: "you seem to think there are only two layers, when
    they are infinitely [nested as] need[ed] to resolve our work", and
    "bot -> (agentic flow state | bot) is progressive and infinitely
    hierarchical as needed."

    Before this, ONLY the conductor's 10 states carried linked_workflow_id,
    from a hardcoded ternary chain in get_workflows. Every behaviour step
    was therefore a leaf, and the tree could never be deeper than
    conductor -> behaviour -> steps. A step declares its own link now, so
    depth is bounded by the work rather than by the renderer.
    """
    block = (Path(__file__).resolve().parent.parent.parent
             / "prism_service/api/workflows.py").read_text(encoding="utf-8")
    assert 'step.get("linkedWorkflowId")' in block, (
        "behaviour steps still cannot declare a deeper link, so the tree is "
        "capped at two levels no matter how the work decomposes")
    # It must come from the step ITSELF, never a fixed list of known ids.
    # Checked against the expression only -- an earlier version of this
    # assertion sliced a fixed 200-char window and tripped on an `==` in the
    # comment below it, which is the same fixed-window fragility this file
    # already corrected once in the p95 test.
    k = block.index('step.get("linkedWorkflowId")')
    expr_end = block.index("),", k)
    expr = block[k:expr_end]
    assert "==" not in expr and "if " not in expr, (
        f"the deeper link is matched against hardcoded ids: {expr!r}")


def test_the_canvas_walks_any_depth_already():
    """The navigation model was never the cap: workflowPath is an appended
    array with per-level breadcrumbs, so once links exist at depth the
    canvas already drills and returns correctly."""
    src = (Path(__file__).resolve().parent.parent.parent
           / "prism_service/web/src/pages/WorkflowsPage.tsx").read_text(encoding="utf-8")
    assert "[...workflowPath, {" in src, "drilling does not append a level"
    assert "workflowPath.slice(0, pathIndex)" in src, (
        "returning to an ancestor does not truncate the path")
