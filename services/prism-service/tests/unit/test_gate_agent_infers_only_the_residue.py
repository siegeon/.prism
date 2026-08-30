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
def test_adjudication_is_expressed_as_named_flow_states():
    """Owner 2026-08-29: "when you codify you better be making new agentic
    states in the flow ... bot -> (agentic flow state | bot) is progressive
    and infinitely hierarchical as needed."

    So the gate's work is a BEHAVIOUR with named states on the Workflows
    page, not a private helper a seat calls where nobody can see it.
    """
    import json
    root = Path(__file__).resolve().parents[4]
    doc = json.loads(
        (root / ".prism/behaviors/conductor/gate-adjudication.json")
        .read_text(encoding="utf-8"))
    assert doc["botId"] == "conductor"
    ids = [s["id"] for s in doc["steps"]]
    assert ids == ["codified", "infer"], ids
    # the deterministic layer must come FIRST in the flow, not after
    assert ids.index("codified") < ids.index("infer")


def test_the_behaviour_nests_under_the_conductor_bot():
    src = (Path(__file__).resolve().parent.parent.parent
           / "prism_service/api/workflows.py").read_text(encoding="utf-8")
    i = src.index("_CONDUCTOR_LINKED_BEHAVIOR_IDS = (")
    block = src[i:i + 400]
    assert '"gate-adjudication"' in block, (
        "the behaviour exists but does not nest under conductor, so it "
        "renders as a disconnected top-level sibling")


def test_the_behaviour_is_declared_on_the_conductor_bot_fsm():
    """A behaviour FILE alone is invisible.

    The catalog enumerates fsm.behaviorIds out of bot.json (api/workflows.py
    walks `bot["fsms"][*]["behaviorIds"]` and fetches each by id), so a JSON
    file dropped into .prism/behaviors/conductor/ that no FSM declares never
    reaches the Workflows page at all. Measured 2026-08-29: the file existed
    and was in _CONDUCTOR_LINKED_BEHAVIOR_IDS, and `GET /api/workflows`
    still listed 11 conductor children without it.
    """
    import json
    root = Path(__file__).resolve().parents[4]
    bot = json.loads(
        (root / ".prism/behaviors/conductor/bot.json").read_text(encoding="utf-8"))
    declared = {b for f in bot.get("fsms", [])
                for b in (f.get("behaviorIds") or [])}
    assert "gate-adjudication" in declared, (
        "the gate-adjudication behaviour is not declared on any conductor "
        f"FSM, so it never renders; declared = {sorted(declared)}")


def test_every_conductor_behaviour_file_is_declared_by_the_bot():
    """Generalises the case above: no behaviour file may be orphaned."""
    import json
    root = Path(__file__).resolve().parents[4]
    bdir = root / ".prism/behaviors/conductor"
    bot = json.loads((bdir / "bot.json").read_text(encoding="utf-8"))
    declared = {b for f in bot.get("fsms", [])
                for b in (f.get("behaviorIds") or [])}
    on_disk = {p.stem for p in bdir.glob("*.json") if p.stem != "bot"}
    orphans = sorted(on_disk - declared)
    assert not orphans, (
        f"behaviour files no FSM declares, so they never render: {orphans}")


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
