"""RED pin — a rollup receipt must never close a human gate (task 457b38db).

Incident (mx-7e03ff, 2026-08-10/11): a proof_type=demo/review root task with
all children done+strong closed via gate_decide's ``elif rollup_ok:`` consume
site (conductor_service.py ~3535-3543) purely on CHILD COMPLETENESS — the
epic's OWN human-judgment oracle (e.g. "two members each see their own work")
was never exercised, and the audit row reads ``epic-rollup=pass`` as though
child status were machine evidence for a gate whose proof burden is a
person's judgment. ``epic_rollup_verdict`` itself stays a pure child-
completeness helper by design (test_seedable_gates.py AC-4) — the fix belongs
at the CONSUME SITE, which must consult proof_type/is_human_judgment before
trusting rollup_ok alone.

Pins:
  * AC-1 — a demo-proof epic with all children done+strong must NOT record
    "epic-rollup=pass" as the reason a plain approve closed its green_gate.
  * AC-2 — same for proof_type=review.
  * AC-3 — reproduces the historical incident shape verbatim (the frontend's
    pre-filled "fresh passing oracle receipt (epic-rollup)" reason) and pins
    that the proof_type check is not bypassed by that wording.
  * AC-4 (belt-and-suspenders, req 3) — the machine adjudicator seat already
    abstains for ANY task with children; pin it stays true for a demo epic.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def _demo_epic(task_svc, proof_type, oracle):
    """A root task PARKED pending at green_gate with a human-judgment
    oracle, plus 2 children carrying strong, done completion_proof — the
    exact shape epic_rollup_verdict scores as rollup_ok=True."""
    parent = task_svc.create(title="human-judgment epic", oracle=oracle,
                             proof_type=proof_type)
    task_svc.update(parent.id, workflow_step="green_gate", gate_state="pending")
    for i in range(2):
        c = task_svc.create(title=f"child {i}", parent_id=parent.id)
        task_svc.update(
            c.id, status="done",
            completion_proof=f"pytest -q: 5 passed; screenshot :8888/c{i}.png")
    return parent


def _last_gate_decide_details(task_svc, task_id):
    joined = " ".join(str(h) for h in task_svc.history(task_id))
    return joined


# ── AC-1 / AC-2 — bare rollup must never be the recorded reason ─────────

def test_demo_epic_rollup_alone_never_closes_the_gate_by_rollup_reason(
        tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _demo_epic(
        task_svc, "demo",
        oracle="two members each confirm their own work on screen")
    res = cond.gate_decide(
        parent.id, "approve",
        reason="all children look done to me",
        actor="distinct-reviewer", session_id="distinct-sess")
    joined = _last_gate_decide_details(task_svc, parent.id)
    assert "epic-rollup=pass" not in joined, (
        "a demo (human-judgment) epic's green_gate must never be closed by "
        f"bare child-rollup completeness alone; res={res}\nhistory={joined}")


def test_review_epic_rollup_alone_never_closes_the_gate_by_rollup_reason(
        tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _demo_epic(
        task_svc, "review",
        oracle="a second person reviews the change off-machine and signs")
    res = cond.gate_decide(
        parent.id, "approve",
        reason="all children look done to me",
        actor="distinct-reviewer", session_id="distinct-sess")
    joined = _last_gate_decide_details(task_svc, parent.id)
    assert "epic-rollup=pass" not in joined, (
        "a review (human-judgment) epic's green_gate must never be closed "
        f"by bare child-rollup completeness alone; res={res}\n"
        f"history={joined}")


# ── AC-3 — reproduce the historical incident shape verbatim ─────────────

def test_reproduces_64ba4755_shape_proof_type_check_not_bypassed(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _demo_epic(
        task_svc, "demo",
        oracle="the customer sees the feature live and confirms")
    # TaskDetailPage.tsx's pre-filled approve reason — this is the literal
    # wording a distinct actor (human click or browser-driving agent) sent
    # in the incident.
    incident_reason = (
        "Approving on live evidence: fresh passing oracle receipt "
        "(epic-rollup) for all children")
    res = cond.gate_decide(
        parent.id, "approve", reason=incident_reason,
        actor="distinct-reviewer", session_id="distinct-sess")
    joined = _last_gate_decide_details(task_svc, parent.id)
    assert "epic-rollup=pass" not in joined, (
        "the incident's own pre-filled reason text must not make the "
        f"rollup branch a valid proof_type-blind pass; res={res}\n"
        f"history={joined}")


# ── AC-4 — belt-and-suspenders: adjudicator seat abstains for epics ─────

def test_adjudicator_seat_abstains_for_demo_epic_with_children(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _demo_epic(
        task_svc, "demo", oracle="a person confirms the demo on screen")
    res = cond.adjudicate_green_gate(parent.id)
    assert res is None, (
        "the machine adjudicator seat must abstain for ANY task with "
        f"children, demo/review epics included; got {res}")
