"""task 54585a5f - a PARKED green_gate must state the REAL refusal.

`api/conductor_flow._autoclear_machine_gate` wrote one fixed prompt --
"awaiting your sign-off - the evidence is ready; review it and Approve" --
every time the machine seat declined a green_gate, and never read WHY it
declined. Its only guard was that `task.gate_reason` was still blank.

Observed live on task 72ccaf94 (2026-08-29): GET /api/conductor/gate/
readiness answered `status=not_shipped`, "this task's [task:72ccaf94]
commit trailer is not yet reachable from origin/main", while the card told
the owner the evidence was ready. No Approve makes a branch reachable from
origin/main, so the click the card asked for could not exist. The owner
read the card and asked why the app wanted a review.

This is task e0149f1f's lesson again: a tooth that COMPUTES a refusal and
then discards it has only half-shipped. The seat must record what it
computed -- and it must keep the genuine sign-off prompt for the gates that
really are a human's, because a demo/review proof_type is human-only by
owner rule eaafdf75 and `adjudicate_green_gate` abstains on it BY DESIGN.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_TASK_ID = "54585a5f-4d9b-4d11-8232-d19822d90d92"

# The real `ConductorService._unshipped_gate_reason` string, ASCII-folded.
_UNSHIPPED = (
    "green_gate: this task's [task:54585a5f] commit trailer is not yet "
    "reachable from origin/main - DONE means SHIPPED; merge/land the "
    "branch before full_outcome_complete/status=done can be set"
)
# The real `ConductorService._oracle_receipt_refusal` shape, ASCII-folded.
_NO_RECEIPT = (
    "green_gate: oracle not evidenced - no EvidenceReceipt on file, the "
    "oracle was never run. The token proof scorer is advisory only."
)
# What the seat used to write unconditionally.
_OLD_PROMPT = ("awaiting your sign-off - the evidence is ready; review it "
               "and Approve (the single human decision for this ticket)")
# The phrase that must never co-exist with an outstanding refusal (R2/AC-3).
_CONTRADICTED = "the evidence is ready"


class _Task:
    """A task parked at green_gate. Only the attributes the seat reads."""

    def __init__(self, **kw):
        self.id = _TASK_ID
        self.title = "parked gate probe"
        self.workflow = "implement"
        self.workflow_step = "green_gate"
        self.gate_state = "pending"
        self.gate_reason = ""
        self.status = "in_progress"
        self.parent_id = ""
        self.proof_type = "test"
        self.oracle = "the pinned suite passes"
        self.verify = ["services/prism-service/tests/unit/"
                       "test_parked_gate_states_the_real_refusal.py"]
        self.likely_misfire = ""
        self.tags = []
        self.completion_proof = ""
        self.plan_doc = ""
        self.plan_diagram = ""
        self.allowed_files = []
        for k, v in kw.items():
            setattr(self, k, v)


class _TaskSvc:
    def __init__(self, task):
        self._t = task
        self.updates: list[dict] = []

    def get(self, task_id):
        return self._t

    def update(self, task_id, **kw):
        self.updates.append(dict(kw))
        for k, v in kw.items():
            setattr(self._t, k, v)
        return self._t

    def list(self, **kw):
        return []


class _Svc:
    """ConductorService stand-in. The machine seat DECLINES (returns None),
    and the two refusal teeth answer whatever the case under test set up."""

    def __init__(self, task, ship_reason="", receipt_refusal=""):
        self._task_svc = _TaskSvc(task)
        self._ship_reason = ship_reason
        self._receipt_refusal = receipt_refusal

    def adjudicate_green_gate(self, task_id, mint=True):
        return None

    def _unshipped_gate_reason(self, task):
        return self._ship_reason

    def _oracle_receipt_refusal(self, task, *, override, reason):
        return self._receipt_refusal, None


def _park(monkeypatch, svc):
    """Run the REAL production seat over a declining machine adjudicator and
    return what it wrote to task.gate_reason."""
    from prism_service.api import conductor_flow
    from prism_service.services import gate_adjudicator

    # The seat is opted in here, so adjudicate_green_gate is actually asked
    # (and, in these cases, declines) rather than skipped.
    monkeypatch.setattr(gate_adjudicator, "is_enabled", lambda: True)
    out = conductor_flow._autoclear_machine_gate(svc, svc._task_svc._t.id)
    assert out is None, "a declined gate must never be auto-approved"
    t = svc._task_svc._t
    assert t.gate_state == "pending", t.gate_state
    return str(t.gate_reason or "")


def test_an_unshipped_gate_names_the_unshipped_trailer(monkeypatch):
    """AC-1. The 72ccaf94 shape: the receipt is fine, the branch is simply
    not landed. The parked gate must say THAT, because no Approve can make
    a commit trailer reachable from origin/main."""
    svc = _Svc(_Task(), ship_reason=_UNSHIPPED, receipt_refusal="")
    reason = _park(monkeypatch, svc)

    assert "[task:54585a5f]" in reason, reason
    assert "not yet reachable from origin/main" in reason, reason
    # AC-3: the contradicted phrase never survives an outstanding refusal.
    assert _CONTRADICTED not in reason.lower(), reason
    # R4: the reason names what to do, so a driver self-diagnoses.
    assert "merge/land the branch" in reason, reason


def test_a_gate_that_truly_needs_a_human_still_asks_for_the_click(monkeypatch):
    """AC-2 / R3, and the pre-declared misfire. A demo proof_type green_gate
    is HUMAN-ONLY by owner rule eaafdf75: adjudicate_green_gate abstains on
    it BY DESIGN, so `declined` here does NOT mean `broken`.

    Both refusal teeth are armed in this case on purpose - a demo oracle has
    no machine receipt, so the receipt tooth always has something to say. The
    fix must not let either of them eat the one prompt that is correct."""
    svc = _Svc(
        _Task(proof_type="demo", verify=[],
              oracle="open the task page and watch the parked gate name "
                     "its own refusal"),
        ship_reason=_UNSHIPPED,
        receipt_refusal=_NO_RECEIPT,
    )
    reason = _park(monkeypatch, svc)

    assert "Approve" in reason, reason
    assert "sign-off" in reason, reason
    # The machine teeth must not have overwritten the human's prompt.
    assert "[task:54585a5f]" not in reason, reason
    assert "no EvidenceReceipt on file" not in reason, reason


def test_an_outstanding_refusal_never_says_the_evidence_is_ready(monkeypatch):
    """AC-3 on the ORACLE lane, and on the stale prompt already on file.

    A gate that parked under the old build carries the fixed prompt in
    task.gate_reason. The blank-only guard meant that string then survived
    forever, because gate_adjudicator._write_pending_reason backs off on an
    unchanged task and the seat itself refuses to touch a non-blank reason.
    A stored prompt that the live refusal contradicts must be replaced."""
    svc = _Svc(_Task(gate_reason=_OLD_PROMPT), receipt_refusal=_NO_RECEIPT)
    reason = _park(monkeypatch, svc)

    assert "no EvidenceReceipt on file" in reason, reason
    assert _CONTRADICTED not in reason.lower(), reason

    # ...and with NO refusal outstanding the prompt is correct and stays.
    ok = _Svc(_Task(), ship_reason="", receipt_refusal="")
    kept = _park(monkeypatch, ok)
    assert "Approve" in kept and "sign-off" in kept, kept
