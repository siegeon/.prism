"""The triage workflow's `decide` gate has a machine seat (task edeab040).

Before this, `decide` was declared a gate with validation=None, no rubric
scored it, gate_adjudicator swept only green/red/story/plan, and
gate_agent's _BEHAVIOUR_FOR_GATE mapped only those same four. A triage task
reached `decide` and stopped: the task page rendered no approve control and
no machine seat had the remit, so the row parked indefinitely.

These tests pin the seat AND its refusals. A gate with nothing to check
must not auto-pass -- the seat exists to judge the classification, not to
stamp it.
"""
from prism_service.services import triage_decision as td


class FakeTask:
    def __init__(self, **kw):
        self.completion_proof = ""
        self.premise_notes = ""
        self.plan_doc = ""
        self.workflow = "triage"
        self.workflow_step = "decide"
        self.gate_state = "pending"
        self.gate_reason = ""
        self.__dict__.update(kw)


GOOD = "Open. The rule fires on 16 live records and the count is rising."


def test_a_bucketed_and_reasoned_classification_passes():
    ok, reason = td.score(FakeTask(completion_proof=GOOD))
    assert ok is True
    assert "open" in reason


def test_an_empty_classification_is_refused():
    ok, reason = td.score(FakeTask())
    assert ok is False
    assert "recorded no classification" in reason


def test_a_classification_naming_no_bucket_is_refused():
    ok, reason = td.score(FakeTask(completion_proof="x" * 90))
    assert ok is False
    assert "names no bucket" in reason


def test_a_classification_naming_two_buckets_is_refused():
    ok, reason = td.score(
        FakeTask(completion_proof="Open or Dropped, " + "y" * 60))
    assert ok is False
    assert "ambiguous" in reason
    assert "open, dropped" in reason


def test_a_bucket_with_no_reason_is_refused():
    ok, reason = td.score(FakeTask(completion_proof="Open"))
    assert ok is False
    assert "gives no reason" in reason


def test_every_refusal_states_a_reason():
    """A tooth that computes a refusal and discards it leaves the driver an
    empty gate_reason and nothing to act on (task e0149f1f)."""
    for task in (FakeTask(),
                 FakeTask(completion_proof="x" * 90),
                 FakeTask(completion_proof="Open"),
                 FakeTask(completion_proof="Open or Dropped, " + "y" * 60)):
        ok, reason = td.score(task)
        assert ok is False
        assert reason.strip(), "a refusal must say why"


def test_the_word_boundary_does_not_match_a_longer_word():
    """'openly' is not the bucket 'open'."""
    assert td.named_buckets("openly stated") == []
    assert td.named_buckets("Open.") == ["open"]
