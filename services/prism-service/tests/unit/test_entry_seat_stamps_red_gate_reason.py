"""task 63c03465, AC-9 - the ENTRY seat must stamp a parked red_gate's reason.

`api/conductor_flow._autoclear_machine_gate` runs at flow entry. When it meets
a pending green_gate that `adjudicate_green_gate` declines, it computes
`_parked_green_gate_reason` and writes it to the row at once
(conductor_flow.py:441-462, task 54585a5f), so the driver's FIRST poll reads
why the gate parked.

The red_gate branch (conductor_flow.py:463-468) does not. It asks
`svc.adjudicate_demo_red_gate` and returns. When that seat abstains and
returns None, no reason is computed and none is written, so the row keeps a
blank `gate_reason` until the `gate_adjudicator` sweep runs - 60 s later on
this host (PRISM_GATE_ADJUDICATOR_INTERVAL=60). For that whole gap the driving
agent reads `pending` + `""` and has nothing to act on. That is the
empty-reason failure class task e0149f1f closed, one gate over from the one
task 8f48f9bb fixed.

The reason ALREADY EXISTS: `gate_adjudicator._pending_decline_reason(svc,
task, "red_gate", project)` reads the latest non-passing EvidenceReceipt
(gate_adjudicator.py:94-98). The entry seat never asks for it.

These tests drive the REAL production seat over a declining red adjudicator.
They are red at base commit 5a7f0e396f35.
"""
from __future__ import annotations

import sys
from pathlib import Path
_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_TASK_ID = "63c03465-5329-4aab-a853-867a5e03eb36"
_PROJECT = "prism"

# The literal reason a real non-passing red EvidenceReceipt carries. The
# assertions below check for THIS text, never for the helper applied to its
# own input - a `stored == normalize(input)` test proves nothing (5de57583).
_RED_REFUSAL = (
    "red_gate: the pinned suite exited rc=0 at tree 5a7f0e39 - a red step "
    "needs rc=1, a run in which the new tests actually fail"
)


class _Receipt:
    """The two EvidenceReceipt fields the red branch reads."""

    def __init__(self, passed, reason):
        self.passed = passed
        self.reason = reason
class _Task:
    """A task parked at red_gate. Only the attributes the seat reads."""

    def __init__(self, **kw):
        self.id = _TASK_ID
        self.title = "entry seat red_gate probe"
        self.workflow = "implement"
        self.workflow_step = "red_gate"
        self.gate_state = "pending"
        self.gate_reason = ""
        self.status = "in_progress"
        self.parent_id = ""
        self.proof_type = "demo"
        self.oracle = "gate_reason names the parked gate's real refusal"
        self.verify = ["services/prism-service/tests/unit/"
                       "test_entry_seat_stamps_red_gate_reason.py"]
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
    """ConductorService stand-in whose demo-red seat ABSTAINS - the documented
    outcome for a ticket the machine cannot judge."""

    def __init__(self, task):
        self._task_svc = _TaskSvc(task)
        self._project_name = _PROJECT
        self.asked = 0

    def adjudicate_demo_red_gate(self, task_id):
        self.asked += 1
        return None
def _park(monkeypatch, svc, receipt):
    """Run the REAL entry seat with the red adjudicator declining, and return
    the row it left behind."""
    from prism_service.api import conductor_flow
    from prism_service.services import gate_adjudicator, oracle_spec

    # Opted in, so the demo-red seat is actually asked (and here declines)
    # rather than skipped.
    monkeypatch.setattr(gate_adjudicator, "is_enabled", lambda: True)
    monkeypatch.setattr(oracle_spec, "latest_receipt",
                        lambda project, task_id: receipt)
    out = conductor_flow._autoclear_machine_gate(svc, svc._task_svc._t.id)
    assert out is None, "an abstained red_gate must never be auto-decided"
    return svc._task_svc._t


def test_a_declined_red_gate_is_stamped_with_the_receipt_refusal(monkeypatch):
    """AC-9. The entry seat records the refusal the red receipt already
    carries, instead of leaving the driver a blank for the whole 60 s gap
    until the next adjudicator sweep."""
    svc = _Svc(_Task())
    t = _park(monkeypatch, svc, _Receipt(False, _RED_REFUSAL))

    assert svc.asked == 1, "the machine seat must still be asked first"
    assert "exited rc=0 at tree 5a7f0e39" in t.gate_reason, t.gate_reason
    assert "needs rc=1" in t.gate_reason, t.gate_reason
def test_the_stamp_holds_the_gate_pending_and_writes_nothing_else(monkeypatch):
    """The seat records a reason and nothing more: it never advances the gate
    as a side effect of writing why it parked, and it touches no other field
    (the `_write_pending_reason` contract, gate_adjudicator.py:108-121)."""
    svc = _Svc(_Task())
    t = _park(monkeypatch, svc, _Receipt(False, _RED_REFUSAL))

    assert t.gate_state == "pending", t.gate_state
    assert t.workflow_step == "red_gate", t.workflow_step
    assert [sorted(u) for u in svc._task_svc.updates] == [["gate_reason"]], (
        svc._task_svc.updates)
    assert "needs rc=1" in t.gate_reason, t.gate_reason


def test_a_red_gate_already_holding_that_reason_is_not_re_stamped(monkeypatch):
    """An unchanged refusal writes nothing, so the entry hook cannot churn the
    row on every poll. Guard, not the observation: it also passes at the base
    commit, where the seat writes nothing at all."""
    svc = _Svc(_Task(gate_reason=_RED_REFUSAL))
    t = _park(monkeypatch, svc, _Receipt(False, _RED_REFUSAL))

    assert svc._task_svc.updates == [], svc._task_svc.updates
    assert t.gate_reason == _RED_REFUSAL, t.gate_reason
