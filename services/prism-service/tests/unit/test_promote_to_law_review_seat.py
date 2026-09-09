"""The promote_to_law workflow's `review` gate has a machine seat (task 23019de9).

Before this, `review` was declared a gate with validation=None, no rubric
scored it, gate_adjudicator swept only green/red/story/plan/decide, and
a promote_to_law task reached `review` and stopped: the task parked at the
gate with no machine seat to decide it, forcing manual intervention.

These tests pin the seat AND its refusals. A gate with nothing to check
must not auto-pass -- the seat exists to judge the draft, not to stamp it.
"""
from prism_service.services import promote_to_law_review as plr


class FakeTask:
    def __init__(self, **kw):
        self.completion_proof = ""
        self.plan_doc = ""
        self.workflow = "promote_to_law"
        self.workflow_step = "review"
        self.gate_state = "pending"
        self.gate_reason = ""
        self.__dict__.update(kw)


GOOD_RULE_TTL = """\
@prefix o: <urn:prism:onto:> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

o:test-rule.target a sh:NodeShape ;
    sh:targetClass o:Module ;
    sh:sparql o:test-rule .

o:test-rule a sh:SPARQLConstraint ;
    rdfs:comment "Test rule" ;
    sh:name "Test" ;
    sh:description "Test constraint" ;
    sh:message "test message" ;
    o:derivedFrom <urn:prism:onto:instance/memory/test> ;
    sh:select "SELECT $this WHERE { $this a o:Module } " .
"""

GOOD_TERM_TTL = """\
@prefix o: <urn:prism:onto:> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<urn:prism:onto:term/test> a o:Term ;
    rdfs:label "Test Term" ;
    rdfs:comment "A test term definition" ;
    o:derivedFrom <urn:prism:onto:instance/memory/test> .
"""


def test_a_valid_shacl_rule_passes():
    ok, reason = plr.score(FakeTask(completion_proof=GOOD_RULE_TTL))
    assert ok is True
    assert "SHACL" in reason or "valid" in reason.lower()


def test_a_valid_lexicon_term_passes():
    ok, reason = plr.score(FakeTask(completion_proof=GOOD_TERM_TTL))
    assert ok is True
    assert "term" in reason.lower() or "valid" in reason.lower()


def test_an_empty_draft_is_refused():
    ok, reason = plr.score(FakeTask())
    assert ok is False
    assert "recorded no output" in reason


def test_a_draft_that_is_not_valid_ttl_is_refused():
    bad_ttl = "@prefix o: broken ttl with no closing"
    ok, reason = plr.score(FakeTask(completion_proof=bad_ttl))
    assert ok is False
    assert "not valid" in reason.lower() or "parse" in reason.lower()


def test_a_rule_missing_the_required_class_declaration_is_refused():
    # Valid TTL but lacks required sh:NodeShape or o:Term class
    incomplete_rule = """\
@prefix o: <urn:prism:onto:> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

o:test a rdfs:Resource ;
    rdfs:label "Just a resource" .
"""
    ok, reason = plr.score(FakeTask(completion_proof=incomplete_rule))
    assert ok is False
    assert "declare" in reason.lower() or "missing" in reason.lower()


def test_every_refusal_states_a_reason():
    """A tooth that computes a refusal and discards it leaves the driver an
    empty gate_reason and nothing to act on (task e0149f1f)."""
    for task in (FakeTask(),
                 FakeTask(completion_proof="@prefix o: broken"),
                 FakeTask(completion_proof="x" * 50),
                 FakeTask(completion_proof="@prefix o: <urn:prism:onto:> . o:test a rdfs:Resource .")):
        ok, reason = plr.score(task)
        assert ok is False
        assert reason.strip(), "a refusal must say why"


def test_the_seat_has_no_remit_outside_its_workflow():
    """The seat must never decide a gate belonging to another workflow."""
    # Non-promote_to_law workflow
    task = FakeTask(workflow="triage")
    result = plr.adjudicate(None, MockTaskSvc(task), "test-id")
    assert result is None

    # Wrong workflow step
    task = FakeTask(workflow="promote_to_law", workflow_step="draft")
    result = plr.adjudicate(None, MockTaskSvc(task), "test-id")
    assert result is None

    # Non-pending gate state
    task = FakeTask(workflow="promote_to_law", workflow_step="review", gate_state="passed")
    result = plr.adjudicate(None, MockTaskSvc(task), "test-id")
    assert result is None


class MockTaskSvc:
    def __init__(self, task):
        self._task = task

    def get(self, task_id):
        return self._task

    def update(self, task_id, **kwargs):
        pass
