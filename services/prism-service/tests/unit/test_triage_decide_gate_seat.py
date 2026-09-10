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


def test_triage_bucketed_is_present_in_verifier_rules():
    """The conductor service wires triage_bucketed as a validation kind."""
    from prism_service.services.conductor_service import ConductorService
    assert "triage_bucketed" in ConductorService._VERIFIER_RULES
    assert ConductorService._VERIFIER_RULES["triage_bucketed"].get("handler") == "triage_bucketed"


def test_wired_rule_returns_verified_true_for_bucketed_classification(tmp_path):
    """The gate verification path returns verified=True with the scorer's reason."""
    from prism_service.services.conductor_service import ConductorService

    service = ConductorService(str(tmp_path / "scores.db"), enable_engine=False)
    task = FakeTask(completion_proof=GOOD)

    result = service._verify_gate(task, "decide", proof_type=None)
    assert result["verified"] is True
    assert "open" in result["reason"]
    assert result["validation"] == "triage_bucketed"


def test_wired_rule_returns_verified_false_for_empty_classification(tmp_path):
    """The gate verification path returns verified=False with reason for empty classification."""
    from prism_service.services.conductor_service import ConductorService

    service = ConductorService(str(tmp_path / "scores.db"), enable_engine=False)
    task = FakeTask(completion_proof="")

    result = service._verify_gate(task, "decide", proof_type=None)
    assert result["verified"] is False
    assert result["reason"]
    assert result["reason"].strip()
    assert "recorded no classification" in result["reason"]


def test_decide_gate_has_validation_none():
    """models/workflow.py TRIAGE_STEPS decide gate has validation=None (the
    revert of the bad commit 830bc54e). A validation value of "triage_bucketed"
    on decide is wrong — it already inherits that from classify via
    _validation_for_gate's backward walk."""
    from prism_service.models.workflow import TRIAGE_STEPS

    decide_step = next((s for s in TRIAGE_STEPS if s["id"] == "decide"), None)
    assert decide_step is not None
    assert decide_step.get("validation") is None


def test_failed_decide_gate_with_machine_refusal_is_resweepable(tmp_path):
    """A FAILED decide gate whose latest gate_decide history row contains
    NO action=reject (meaning a machine/config refusal) is eligible for
    re-sweep by _failed_gate_is_refused_approve."""
    from prism_service.services.conductor_service import ConductorService

    service = ConductorService(str(tmp_path / "scores.db"), enable_engine=False)

    # Mock the history to return a gate_decide with NO action=reject
    # (a configuration refusal, not a human decision).
    service._task_svc = _FakeTaskService([
        "gate_decide event: gate=decide, actor=triage-validation, outcome=refused"
    ])

    result = service._failed_gate_is_refused_approve("task-123", "decide")
    assert result is True, "should be resweepable when the refusal was a machine/config issue"


def test_failed_decide_gate_with_human_reject_is_not_resweepable(tmp_path):
    """A FAILED decide gate whose latest gate_decide history row contains
    action=reject (meaning a human explicitly rejected it) must NOT be
    re-swept."""
    from prism_service.services.conductor_service import ConductorService

    service = ConductorService(str(tmp_path / "scores.db"), enable_engine=False)

    # Mock the history to return a gate_decide with action=reject
    # (human decision, final).
    service._task_svc = _FakeTaskService([
        "gate_decide event: gate=decide, action=reject, actor=owner"
    ])

    result = service._failed_gate_is_refused_approve("task-123", "decide")
    assert result is False, "should NOT be resweepable when a human rejected it"


def test_green_gate_pending_and_failed_behavior_unchanged():
    """Regression guard: green_gate's existing pending+failed re-sweep
    behavior is unchanged by the new decide-gate logic."""
    from prism_service.services.conductor_service import ConductorService

    service = ConductorService(":memory:", enable_engine=False)

    # A machine refusal on green_gate should still be resweepable.
    service._task_svc = _FakeTaskService([
        "gate_decide event: gate=green_gate, actor=conductor-adjudicator, outcome=refused"
    ])

    result = service._failed_gate_is_refused_approve("task-456", "green_gate")
    assert result is True, "green_gate machine refusal should still be resweepable"

    # A human reject on green_gate should still be unresweppable.
    service._task_svc = _FakeTaskService([
        "gate_decide event: gate=green_gate, action=reject, actor=owner"
    ])

    result = service._failed_gate_is_refused_approve("task-789", "green_gate")
    assert result is False, "green_gate human reject should still be unresweppable"


class _FakeTaskService:
    """Minimal fake task service that returns canned history."""
    def __init__(self, history_rows):
        self._history_rows = history_rows

    def history(self, task_id):
        return self._history_rows
