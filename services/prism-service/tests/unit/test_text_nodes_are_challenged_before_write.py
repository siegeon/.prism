"""Every text-generating conductor node is challenged before it writes
(task d7947eb6).

Owner: "we must make sure all of the text generation nodes that hit
artifacts use the ontology rules, they should have separate nodes to
challenge, correct or provide access to prevent this from happening
again."

Four conductor nodes write free text into a task artifact --
review_previous_notes -> premise_notes, draft_story -> plan_doc,
verify_plan -> plan_doc, implement_tasks / verify_green_state ->
completion_proof. services/ste.py normalises on write, but
TaskService._align_plan_doc holds every BULLET, heading and table row
byte-identical (they are rubric-critical line shapes), and a story is
almost entirely bullets -- so a generated AC line kept its semicolon
and its synonym, and the live text-is-plain / text-uses-canonical-terms
counts climbed with every drive.

These tests pin the CHALLENGE node: codified (no model call), judged by
the LIVE ontology rules read off shapes.ttl (never a second hand-written
checker), repairing only what a machine can repair safely, and holding
every hedge and every oracle claim byte-identical.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path


from prism_service.services import text_challenge as tc

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BEHAVIORS = _REPO_ROOT / ".prism" / "behaviors" / "conductor"


# ----------------------------------------------------------------------
# AC-1 -- a generated story lands clean
# ----------------------------------------------------------------------

_STORY_IN = (
    "## Summary\n"
    "\n"
    "The ticket can't land; the queue blocks it.\n"
    "\n"
    "## Acceptance Criteria\n"
    "\n"
    "- AC-1: A ticket that can't render shows an error; the user "
    "retries. — oracle: `curl /api/tasks; grep row`\n"
)

# Written by hand from the spec, NEVER computed by calling the function
# under test on its own input (that assertion proves nothing -- lesson
# from task 5de57583). Semicolon becomes a sentence break, the
# contraction expands, the lexicon synonym "ticket" becomes the
# canonical "Task", the heading stays byte-identical because
# arc_governance._sections keys a rubric section by its exact heading
# text, and everything from the `oracle:` marker rightwards is held
# byte-identical because a repair must never restate an oracle.
_STORY_OUT = (
    "## Summary\n"
    "\n"
    "The Task cannot land. The queue blocks it.\n"
    "\n"
    "## Acceptance Criteria\n"
    "\n"
    "- AC-1: A Task that cannot render shows an error. The user "
    "retries. — oracle: `curl /api/tasks; grep row`\n"
)


def test_a_generated_story_lands_clean():
    """The story draft_story emits carries a semicolon and a synonym;
    what the challenge node stores carries neither, and the live
    ontology rules agree it is clean."""
    before = tc.challenge(_STORY_IN)
    assert not before["ok"], before
    fired = {v["name"] for v in before["violations"]}
    assert "text-is-plain" in fired, before
    assert "text-uses-canonical-terms" in fired, before

    result = tc.correct(_STORY_IN, field="plan_doc")
    assert result["changed"] is True
    assert result["text"] == _STORY_OUT

    after = tc.challenge(result["text"])
    assert after["ok"], after
    assert after["violations"] == []


def test_an_oracle_claim_survives_the_repair_byte_identical():
    """stop_if: a repair would change an oracle or a stop_if meaning.
    Everything from the `oracle:` marker rightwards is copied through,
    and an oracle FIELD is challenged but never rewritten."""
    line = _STORY_OUT.splitlines()[-1]
    assert line.endswith("— oracle: `curl /api/tasks; grep row`")

    oracle = "The card can't render; (1) the row shows. (2) the count holds."
    res = tc.correct(oracle, field="oracle")
    assert res["text"] == oracle
    assert res["changed"] is False
    assert res["repairable"] is False
    # It is still CHALLENGED -- refusing to repair is not refusing to look.
    assert res["after"]["violations"], res


# ----------------------------------------------------------------------
# AC-2 -- the challenge node is codified
# ----------------------------------------------------------------------


def test_the_challenge_node_calls_no_model():
    """Codified: neither the module nor the route handler reaches
    prism_service.inference, the only path to a model in this service,
    and the node still answers when every model entry point raises."""
    module_src = inspect.getsource(tc)
    assert "prism_service.inference" not in module_src
    assert "claude_cli" not in module_src

    from prism_service.api import workflows as wf

    handler_src = inspect.getsource(wf.workflow_step_text_challenge)
    assert "prism_service.inference" not in handler_src
    assert "claude_cli" not in handler_src

    from prism_service.inference import claude_cli

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("the challenge node called a model")

    original = claude_cli.invoke
    claude_cli.invoke = _boom
    try:
        verdict = tc.challenge("The ticket can't land; it blocks.")
        assert verdict["ok"] is False
        fixed = tc.correct("The ticket can't land; it blocks.",
                           field="completion_proof")
        assert fixed["text"] == "The Task cannot land. It blocks."
    finally:
        claude_cli.invoke = original


def test_the_rules_come_from_the_shapes_file_not_a_second_checker():
    """The rule set is DERIVED from shapes.ttl (the shapes ARE the rule
    catalog) -- a text rule is one whose own SPARQL select reads
    rdfs:label or rdfs:comment, never a hand-kept list in Python."""
    names = tc.text_rule_names()
    assert "text-is-plain" in names
    assert "text-uses-canonical-terms" in names
    # A structural rule targeting the same class is NOT a text rule.
    assert "task-blocked-needs-decomposition" not in names
    assert "task-names-its-channel" not in names

    src = inspect.getsource(tc)
    # No hand-written copy of the rule names anywhere in the module.
    assert src.count('"text-is-plain"') == 0
    assert src.count('"text-uses-canonical-terms"') == 0


# ----------------------------------------------------------------------
# AC-3 -- a repair keeps every hedge
# ----------------------------------------------------------------------


def test_a_repair_keeps_every_hedge():
    """Standing project rule: turning "may have failed" into "failed" is
    a different claim, not a simplification."""
    text = "The run may have failed; we can't tell yet."
    result = tc.correct(text, field="completion_proof")
    assert result["text"] == "The run may have failed. We cannot tell yet."
    assert result["hedges_kept"] is True

    for hedge in ("may", "might", "could", "sometimes", "possibly", "likely"):
        probe = f"The seat {hedge} refuse; the driver can't tell."
        out = tc.correct(probe, field="completion_proof")["text"]
        assert f" {hedge} " in out, out


def test_a_repair_that_would_drop_a_hedge_is_refused():
    """The hedge guard is a real tooth, not only a test: a repairer that
    dropped a hedge word makes correct() return the ORIGINAL text with a
    named refusal."""
    import prism_service.services.lexicon as lexicon

    original = lexicon.align

    def _drop_hedge(text):
        return text.replace("may ", ""), [{"from": "may", "to": ""}]

    lexicon.align = _drop_hedge
    try:
        text = "The run may have failed."
        result = tc.correct(text, field="completion_proof")
        assert result["text"] == text
        assert result["changed"] is False
        assert result["hedges_kept"] is False
        assert "hedge" in result["refused"].lower()
    finally:
        lexicon.align = original


# ----------------------------------------------------------------------
# Registration -- the node is visible and it is wired into the write path
# ----------------------------------------------------------------------


def test_every_text_generating_step_has_a_challenge_node_after_it():
    """The Workflows page shows a challenge node after each text node.
    Each behaviour's own steps array is what the page renders as that
    behaviour's nodes (api/workflows._conductor_behavior_workflows), so
    the challenge step lives there, immediately after the step that
    generates the text -- the same shape review_previous_notes already
    uses for its codified premise-citation-check."""
    expected = {
        "review-previous-notes-loop": "review_previous_notes",
        "draft-story-loop": "draft_story",
        "verify-plan-loop": "verify_plan",
        "implement-tasks-loop": "implement_tasks",
        "verify-green-state-loop": "verify_green_state",
    }
    for behavior_id, step_id in expected.items():
        path = _BEHAVIORS / f"{behavior_id}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        ids = [s["id"] for s in doc["steps"]]
        assert "text-challenge" in ids, f"{behavior_id} has no challenge node"
        assert ids[-1] == "text-challenge", (
            f"{behavior_id}: the challenge node must come AFTER the node "
            f"that writes the text, got {ids}")
        step = doc["steps"][-1]
        assert step["kind"] == "http-callback"
        assert "/api/workflows/steps/text-challenge" in step["url"]
        assert step_id in step["body"]


def test_the_catalog_renders_the_challenge_node_after_the_text_node(monkeypatch):
    """The Workflows page reads each behaviour's own steps through
    api/workflows._conductor_behavior_workflows, so drive that real
    builder against the REAL behaviour files on disk (only the external
    AosWorkflows HTTP seam is stubbed) and check the challenge node is
    the last node of each text behaviour's entry."""
    from prism_service.api import workflows as wf
    from prism_service.services import claude_transcripts

    monkeypatch.setattr(claude_transcripts, "_project_source_path",
                        lambda project: str(_REPO_ROOT))

    def _engine(path: str) -> dict:
        route = path.split("?", 1)[0]
        if route.endswith("/bots/conductor"):
            return json.loads(
                (_BEHAVIORS / "bot.json").read_text(encoding="utf-8"))
        behavior_id = route.rsplit("/", 1)[-1]
        return json.loads(
            (_BEHAVIORS / f"{behavior_id}.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(wf, "_workflow_engine_json", _engine)

    entries = {e["id"]: e for e in wf._conductor_behavior_workflows("prism")}
    for behavior_id in ("review-previous-notes-loop", "draft-story-loop",
                        "verify-plan-loop", "implement-tasks-loop",
                        "verify-green-state-loop"):
        nodes = entries[behavior_id]["steps"]
        assert nodes[-1]["id"] == "text-challenge", (
            f"{behavior_id} renders {[n['id'] for n in nodes]}")
        assert nodes[-1]["depends_on"] == [nodes[-2]["id"]], (
            "the challenge node must depend on the node that wrote the text")
        assert "/api/workflows/steps/text-challenge" in nodes[-1]["action"]


def test_the_field_map_covers_every_text_generating_step():
    """Each writing step declares which artifact fields it writes, so
    the challenge reads exactly what the node just produced."""
    assert tc.ARTIFACT_FIELDS["review_previous_notes"] == (
        "premise_notes", "completion_proof")
    assert tc.ARTIFACT_FIELDS["draft_story"] == ("plan_doc", "completion_proof")
    assert tc.ARTIFACT_FIELDS["verify_plan"] == ("plan_doc", "completion_proof")
    assert tc.ARTIFACT_FIELDS["implement_tasks"] == ("completion_proof",)
    assert tc.ARTIFACT_FIELDS["verify_green_state"] == ("completion_proof",)
    for field in ("oracle", "stop_if", "likely_misfire"):
        assert field in tc.NEVER_REPAIR


def test_the_report_path_challenges_before_the_step_advances():
    """conductor_flow.flow_report is the ONE choke point every driver
    passes through (task_runner._route_proof, resume_actuator and the
    MCP conductor_work loop all write the artifact and then report
    here), so the challenge runs there -- before advance_task and
    before any gate rubric reads the artifact."""
    from prism_service.api import conductor_flow

    src = inspect.getsource(conductor_flow.flow_report)
    challenge_at = src.find("challenge_step_artifacts")
    advance_at = src.find("svc.advance_task(")
    assert challenge_at != -1, "flow_report never challenges the artifact"
    assert advance_at != -1
    assert challenge_at < advance_at, (
        "the challenge must run BEFORE the advance, or a gate scores "
        "text the challenge has not seen")


def test_the_node_writes_the_repair_back_through_the_task_service():
    """challenge_step_artifacts repairs in place: it hands the corrected
    text to TaskService.update, so the normal STE pipeline and the
    normal history row still happen -- it never writes a column itself."""
    class _Task:
        id = "t1"
        plan_doc = _STORY_IN
        completion_proof = ""
        oracle = ""
        stop_if: list = []
        likely_misfire = ""
        premise_notes = ""

    writes: list[dict] = []

    class _Svc:
        def get(self, task_id):
            return _Task()

        def update(self, task_id, **kw):
            writes.append(kw)
            return _Task()

    report = tc.challenge_step_artifacts(_Svc(), "t1", "draft_story")
    assert writes == [{"plan_doc": _STORY_OUT}]
    assert report["fields"]["plan_doc"]["changed"] is True
    assert report["fields"]["plan_doc"]["after"]["ok"] is True


def test_a_real_task_service_stores_the_repaired_story(tmp_path):
    """The round trip through the REAL TaskService: draft_story writes a
    story whose acceptance criterion carries a semicolon and a synonym
    (TaskService._align_plan_doc holds a bullet byte-identical, which is
    exactly why the violation used to land), the challenge node runs,
    and what is ON FILE afterwards carries neither -- _apply_ste does not
    undo the repair on the way back in."""
    from prism_service.services.task_service import TaskService

    svc = TaskService(str(tmp_path / "tasks.db"))
    task = svc.create(title="A story lands clean", plan_doc=_STORY_IN)

    stored_before = svc.get(task.id).plan_doc
    assert "can't" in stored_before, "the bullet used to keep its contraction"
    assert "; the user" in stored_before, "the bullet used to keep its semicolon"
    assert " ticket " in stored_before, "the bullet used to keep its synonym"

    report = tc.challenge_step_artifacts(svc, task.id, "draft_story")
    assert report["repaired"] == ["plan_doc"], report
    assert report["unrepaired"] == [], report

    stored_after = svc.get(task.id).plan_doc
    assert stored_after == _STORY_OUT
    assert tc.challenge(stored_after)["ok"] is True


def test_an_unrepairable_violation_is_named_not_swallowed():
    """"provide access": a violation the machine must not repair is
    reported with the rule's own message, so a person or an agent can
    act on it instead of it silently landing."""
    # A semicolon inside a SINGLE-quoted span. shapes.ttl strips fenced
    # code, an inline code span, a double-quoted string and a URL before
    # it looks -- deliberately NARROWER than ste._protected_spans, which
    # also protects a single-quoted string (SPARQL has no lookbehind, so
    # an approximation that under-strips is the safe one). So the rule
    # fires here and the repairer correctly declines to touch it.
    text = "The seat reads 'a; b' now."
    result = tc.correct(text, field="completion_proof")
    assert result["text"] == text
    assert result["after"]["ok"] is False
    messages = [v["message"] for v in result["after"]["violations"]]
    assert any("plain English" in m for m in messages), messages
