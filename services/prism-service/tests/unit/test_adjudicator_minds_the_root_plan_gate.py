"""The adjudicator minds the root plan gate - task 594f9a58, epic 3baadd19.

Owner 2026-08-30: "we moved from here. If we have to escalate, p90 certainty
or whatever, then you can block on the gate, but the ontology and adjudicator
is to mind the gates, we made it a game."

TODAY no seat can decide a ROOT task's plan_gate, however distinct. Both
machine seats park unconditionally on `design_packet.approval_status(...)`:
the entry-time seat `api/conductor_flow._autoclear_machine_gate`
(conductor_flow.py:426-445) and the re-sweep seat
`ConductorService.adjudicate_rubric_gate` (conductor_service.py:3216-3239).
The only route that records the approval,
`POST /api/conductor/design-packet/approve`, refuses unless the resolved
identity is human (api/conductor.py:242), and `design_packet.record_approval`
accepts `method="owner_explicit"` alone (`_ALLOWED_METHODS`,
design_packet.py:29, built for task 98d38111).

This suite pins the change:

  AC-1  `design_packet.plan_gate_certainty` is a REAL, non-constant measure -
        a rich packet scores above a thin one, and each of its four signals
        is computed independently.
  AC-2  a root plan_gate whose certainty reaches the threshold is DECIDED by
        the adjudicator seat with no human action, and the gate_decide row
        names the seat, the method and the certainty.
  AC-3  a packet below the threshold PARKS, and gate_reason names a concrete
        thing the human must judge - never blank, never the generic string
        alone.
  AC-4  98d38111's rule survives: `record_approval` still takes only
        `owner_explicit`, and the seat's certainty approval writes NO row to
        the approvals ledger.
  AC-5  the threshold is configurable through
        PRISM_PLAN_GATE_CERTAINTY_THRESHOLD, defaults to 0.90 on unset or
        invalid input, and BOTH seats reach the same verdict because both
        call the same function.

RED AT BASE (5ea606e8): `design_packet.plan_gate_certainty` and
`design_packet.certainty_threshold` do not exist, and neither seat consults
any certainty measure - every test below fails at the base commit.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# fixtures - mirrors tests/unit/test_root_plan_gate_waits_for_the_user.py's
# _services/_plan_gate_task and test_design_packet_plan_gate.py's dp_env.
# ---------------------------------------------------------------------------

_RICH_PLAN = "\n".join([
    "## Summary",
    "The adjudicator seat scores the design packet it is asked to release "
    "and decides the root plan gate itself once that score clears a "
    "configured threshold. Below the threshold it parks and names what a "
    "human must judge, so the owner keeps the veto they were promised "
    "without being asked to click on every plan a machine could read.",
    "",
    "## Requirements",
    "- A certainty function reads the packet and returns a score, the "
    "signals behind it, and the reasons a low score carries.",
    "- The threshold is configurable and defaults to a high value.",
    "- Both machine seats call the one function, so they cannot disagree.",
    "- The owner approval ledger is untouched by a machine decision.",
    "",
    "## Acceptance Criteria (plan coverage)",
    "AC-1: the certainty measure is real and not a constant.",
    "oracle: two fixture packets of different substance score differently.",
    "AC-2: a packet at or above the threshold clears through the seat.",
    "oracle: the re-sweep seat returns ok and the history row names it.",
    "AC-3: a packet below the threshold parks with a specific reason.",
    "oracle: the stored gate reason repeats one of the measured reasons.",
    "AC-4: the owner approval ledger stays human only and stays empty.",
    "oracle: a machine approval writes no row into the approvals file.",
    "AC-5: the threshold is configurable and both seats agree on it.",
    "oracle: a lowered threshold releases the same packet at both seats.",
])

_RICH_DIAGRAM = "\n".join([
    "flowchart TD",
    '    Root["ROOT task at plan_gate"] --> Entry["entry-time seat"]',
    '    Root --> Resweep["rubric re-sweep seat"]',
    '    Entry --> Certainty["plan_gate_certainty"]',
    '    Resweep --> Certainty',
    '    Certainty --> Score{"score vs threshold"}',
    '    Score -->|"at or above"| Approve["gate_decide approve"]',
    '    Score -->|"below"| Park["park pending with reasons"]',
    '    Approve --> Next["advance past plan_gate"]',
    '    Park --> Human["human approves the packet"]',
    "    Human --> Next",
])

_RICH_ORACLE = (
    "A root task at plan_gate with a complete design packet reaches an "
    "adjudicator decision with no human action, and the history row names "
    "the seat, the method and the certainty.")

_RICH_MISFIRE = (
    "The seat approves every packet because the certainty is a constant, so "
    "the escalation path never fires and the owner loses the plan veto.")


@pytest.fixture()
def dp_env(tmp_path, monkeypatch):
    """Isolated PRISM_DATA_DIR so the approvals JSONL and prototype files
    never touch a real project (same shape as test_design_packet_plan_gate)."""
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    return "adj-plan-" + uuid.uuid4().hex[:8]


def _services(tmp_path, project):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = project
    return task_svc, cond


def _task(task_svc, plan_doc, plan_diagram, oracle, likely_misfire,
          parent_id="", tags=None):
    t = task_svc.create(title="root plan gate probe", oracle=oracle,
                        proof_type="test", tags=tags or [],
                        parent_id=parent_id, workflow="implement")
    task_svc.update(t.id, workflow_step="plan_gate", gate_state="pending",
                    plan_doc=plan_doc, plan_diagram=plan_diagram,
                    likely_misfire=likely_misfire)
    return task_svc.get(t.id)


def _rich_task(task_svc):
    """A ROOT task carrying a full design packet - long plan, substantive
    oracle and likely_misfire, a diagram that parses with many edges, and no
    "ui" tag, so order_report reports no scope gap."""
    return _task(task_svc, _RICH_PLAN, _RICH_DIAGRAM, _RICH_ORACLE,
                 _RICH_MISFIRE)


def _thin_task(task_svc):
    """A ROOT task carrying almost nothing - the shape the existing suite
    tests/unit/test_root_plan_gate_waits_for_the_user.py already parks."""
    return _task(task_svc, "AC-1 covered", "", "oracle text", "")


def _gate_rows(task_svc, task_id):
    return [h for h in task_svc.history(task_id) if h.action == "gate_decide"]


def _stub_rubric_pass(monkeypatch, cond):
    monkeypatch.setattr(
        cond, "_verify_rubric_gate",
        lambda t, validation: {"verified": True, "reason": "stub rubric green",
                               "verifier": None, "validation": validation})


def _stub_entry_seat_pass(monkeypatch, cond):
    """The entry-time seat's own rubric check plus the deterministic plan
    teeth (plan_gate_checks.refusal), so the certainty branch is what the
    test measures rather than an unrelated refusal."""
    from prism_service.services import plan_gate_checks as pgc
    monkeypatch.setattr(
        cond, "_verify_gate",
        lambda t, step_id, proof_type=None: {
            "verified": True, "reason": "stub rubric green",
            "verifier": None, "validation": "plan_coverage"})
    monkeypatch.setattr(pgc, "refusal", lambda task, project="default": "")


# ---------------------------------------------------------------------------
# AC-1 - the certainty measure is real, not a constant.
# ---------------------------------------------------------------------------


def test_certainty_scores_a_rich_packet_above_a_thin_one(dp_env, tmp_path):
    from prism_service.services import design_packet as dp
    task_svc, _ = _services(tmp_path, dp_env)
    rich = dp.plan_gate_certainty(dp_env, _rich_task(task_svc).id,
                                  _rich_task(task_svc))
    thin = dp.plan_gate_certainty(dp_env, _thin_task(task_svc).id,
                                  _thin_task(task_svc))
    assert 0.0 <= thin["score"] <= 1.0 and 0.0 <= rich["score"] <= 1.0
    assert rich["score"] > thin["score"], (rich["score"], thin["score"])
    assert rich["signals"] != thin["signals"]


def test_certainty_computes_each_signal_independently(dp_env, tmp_path):
    from prism_service.services import design_packet as dp
    task_svc, _ = _services(tmp_path, dp_env)
    task = _rich_task(task_svc)
    out = dp.plan_gate_certainty(dp_env, task.id, task)
    signals = out["signals"]
    assert set(signals) == {"plan_completeness", "oracle_quality",
                            "diagram_quality", "scope_alignment"}, signals
    assert len(set(signals.values())) > 1, (
        "four signals that always share one value are one constant wearing "
        "four names")
    assert isinstance(out["reasons"], list)


def test_certainty_reports_a_reason_for_every_missing_signal(dp_env, tmp_path):
    from prism_service.services import design_packet as dp
    task_svc, _ = _services(tmp_path, dp_env)
    task = _thin_task(task_svc)
    out = dp.plan_gate_certainty(dp_env, task.id, task)
    assert out["score"] < 0.9, out["score"]
    assert out["reasons"], "a low score must say what is missing"
    assert all(isinstance(r, str) and r.strip() for r in out["reasons"])


# ---------------------------------------------------------------------------
# AC-2 - the seat decides a high-certainty root plan_gate with no human.
# ---------------------------------------------------------------------------


def test_resweep_seat_releases_a_high_certainty_root_plan(dp_env, tmp_path,
                                                          monkeypatch):
    from prism_service.services.conductor_service import ADJUDICATOR_SEAT
    task_svc, cond = _services(tmp_path, dp_env)
    task = _rich_task(task_svc)
    assert task.parent_id == ""
    _stub_rubric_pass(monkeypatch, cond)

    res = cond.adjudicate_rubric_gate(task.id)

    assert res is not None and res.get("ok") is True, res
    after = task_svc.get(task.id)
    assert after.workflow_step != "plan_gate", after.workflow_step
    assert after.gate_state != "pending", after.gate_state
    rows = _gate_rows(task_svc, task.id)
    assert rows, "the decision must leave a gate_decide history row"
    assert rows[-1].actor == ADJUDICATOR_SEAT, rows[-1].actor
    _row = " ".join(str(getattr(rows[-1], f, "") or "")
                     for f in ("detail", "reason", "notes", "summary"))
    assert "certainty=" in _row, _row


# ---------------------------------------------------------------------------
# AC-3 - a thin packet parks and names what the human must judge.
# ---------------------------------------------------------------------------


def test_a_thin_packet_parks_and_names_what_the_human_must_judge(
        dp_env, tmp_path, monkeypatch):
    from prism_service.services import design_packet as dp
    task_svc, cond = _services(tmp_path, dp_env)
    task = _thin_task(task_svc)
    _stub_rubric_pass(monkeypatch, cond)
    reasons = dp.plan_gate_certainty(dp_env, task.id, task)["reasons"]

    res = cond.adjudicate_rubric_gate(task.id)

    assert res is None, res
    after = task_svc.get(task.id)
    assert after.workflow_step == "plan_gate"
    assert after.gate_state == "pending"
    assert after.gate_reason.strip(), "a parked gate must never read blank"
    assert any(r in after.gate_reason for r in reasons), after.gate_reason
    assert not _gate_rows(task_svc, task.id)


# ---------------------------------------------------------------------------
# AC-4 - task 98d38111's rule survives untouched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["conductor-adjudicator", "machine",
                                    "certainty", "owner_implicit", ""])
def test_record_approval_still_refuses_every_non_owner_method(dp_env, tmp_path,
                                                              method):
    from prism_service.services import design_packet as dp
    task_svc, _ = _services(tmp_path, dp_env)
    task = _rich_task(task_svc)
    with pytest.raises(ValueError):
        dp.record_approval(dp_env, task.id, task, approver="conductor",
                           method=method)


def test_a_machine_certainty_approval_writes_no_approval_row(dp_env, tmp_path,
                                                             monkeypatch):
    from prism_service.services import design_packet as dp
    task_svc, cond = _services(tmp_path, dp_env)
    task = _rich_task(task_svc)
    _stub_rubric_pass(monkeypatch, cond)

    res = cond.adjudicate_rubric_gate(task.id)

    assert res is not None and res.get("ok") is True, res
    assert task_svc.get(task.id).workflow_step != "plan_gate"
    assert dp.read_approvals(dp_env, task.id) == [], (
        "a machine decision must never appear in the owner approval ledger")


# ---------------------------------------------------------------------------
# AC-5 - the threshold is configurable and both seats agree.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["nope", "", "  ", "-0.5", "1.5", "0.9.1"])
def test_threshold_falls_back_to_090_on_invalid_input(monkeypatch, raw):
    from prism_service.services import design_packet as dp
    monkeypatch.setenv("PRISM_PLAN_GATE_CERTAINTY_THRESHOLD", raw)
    assert dp.certainty_threshold() == pytest.approx(0.90)


def test_threshold_defaults_to_090_when_unset(monkeypatch):
    from prism_service.services import design_packet as dp
    monkeypatch.delenv("PRISM_PLAN_GATE_CERTAINTY_THRESHOLD", raising=False)
    assert dp.certainty_threshold() == pytest.approx(0.90)


def test_a_lowered_threshold_releases_a_packet_that_would_park(
        dp_env, tmp_path, monkeypatch):
    task_svc, cond = _services(tmp_path, dp_env)
    task = _thin_task(task_svc)
    _stub_rubric_pass(monkeypatch, cond)
    monkeypatch.setenv("PRISM_PLAN_GATE_CERTAINTY_THRESHOLD", "0.0")

    res = cond.adjudicate_rubric_gate(task.id)

    assert res is not None and res.get("ok") is True, res
    assert task_svc.get(task.id).workflow_step != "plan_gate"


def test_both_seats_reach_the_same_verdict_on_the_same_packet(
        dp_env, tmp_path, monkeypatch):
    from prism_service.api import conductor_flow as cf
    task_svc, cond = _services(tmp_path, dp_env)
    _stub_entry_seat_pass(monkeypatch, cond)
    _stub_rubric_pass(monkeypatch, cond)

    rich_entry, rich_resweep = _rich_task(task_svc), _rich_task(task_svc)
    thin_entry, thin_resweep = _thin_task(task_svc), _thin_task(task_svc)

    assert cf._autoclear_machine_gate(cond, rich_entry.id) is not None
    assert cond.adjudicate_rubric_gate(rich_resweep.id) is not None
    assert cf._autoclear_machine_gate(cond, thin_entry.id) is None
    assert cond.adjudicate_rubric_gate(thin_resweep.id) is None
    assert task_svc.get(thin_entry.id).gate_reason.strip()
