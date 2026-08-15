"""gate_decide persists the REAL deciding actor, not a hardcoded literal
(task 1bc13307).

DEFECT: ConductorService.gate_decide accepts an `actor: Optional[str] =
None` parameter but every branch reassigns the local `actor` variable to a
hardcoded literal before the final `record_history(..., actor=actor)`
write — "manual-override" on the override path, "conductor" on the epic
roll-up and verifier-driven success paths. Every real caller
(adjudicate_test_red_gate / adjudicate_rubric_gate / adjudicate_green_gate
all pass actor=ADJUDICATOR_SEAT; the SPA's Approve button passes a real
human identity) has its actor silently discarded, so
actor_service.resolve("conductor") always resolves to ActorKind.UNKNOWN.

These tests pin that a caller-supplied `actor` is persisted VERBATIM on
the gate_decide history row, across the three paths named by the ticket's
stop_if: override (:3498), epic roll-up (:3511), and the verifier-driven
success path (:3592). They must FAIL today and pass once gate_decide stops
shadowing `actor`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class FakeVerifier:
    """Minimal stand-in — see test_conductor_v2_verifier_gate.py for the
    documented original. Only what these tests need."""

    def __init__(self, result: Optional[dict] = None) -> None:
        self.calls: list[dict] = []
        self.next_result: Optional[dict] = result

    def run(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        assert self.next_result is not None, "FakeVerifier has no result queued"
        return self.next_result


def _services(tmp_path, verifier: Optional[FakeVerifier] = None):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"),
        enable_engine=False,
        task_svc=task_svc,
        verifier_svc=verifier,
    )
    return task_svc, cond


def _workflow():
    from prism_service.models.workflow import WORKFLOW_STEPS

    return WORKFLOW_STEPS


def _gate_id(name: str) -> str:
    return next(s["id"] for s in _workflow()
                if s["type"] == "gate" and s["id"] == name)


def _walk_to_gate(cond, task_id: str, gate_id: str) -> None:
    """Advance a task until its workflow_step equals gate_id, clearing any
    earlier pending gate with a manual override (distinct actor per
    clear — mirrors test_conductor_v2_verifier_gate.py's helper)."""
    steps = _workflow()
    target_idx = next(i for i, s in enumerate(steps) if s["id"] == gate_id)
    cond._task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk pinning gate_decide actor "
        "passthrough - UNVERIFIED\n"))
    guard = (target_idx + 1) * 3
    cleared = 0
    while guard > 0:
        guard -= 1
        snap = cond._task_svc.get(task_id)
        if snap.workflow_step == gate_id and snap.gate_state == "pending":
            return
        if snap.gate_state == "pending":
            cleared += 1
            cond.gate_decide(
                task_id, action="approve",
                reason=("walk_to_gate intermediate; independent re-run: "
                        "pytest -q -> 1 failed"),
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}")
            continue
        cond.advance_task(task_id)


def _last_gate_row(task_svc, task_id: str):
    rows = task_svc.history(task_id)
    gate_rows = [r for r in rows if r.action == "gate_decide"]
    assert gate_rows, "gate_decide must produce a task_history row"
    return gate_rows[-1]


ADJUDICATOR_SEAT = "conductor-adjudicator"


# ----------------------------------------------------------------------
# Verifier-driven success path (conductor_service.py:3592)
# ----------------------------------------------------------------------


def test_verifier_driven_approve_persists_machine_seat_actor(tmp_path):
    """A machine seat (adjudicate_test_red_gate / adjudicate_rubric_gate /
    adjudicate_green_gate all call gate_decide(actor=ADJUDICATOR_SEAT)) must
    be persisted as-is on the history row — not the literal 'conductor'."""
    verifier = FakeVerifier({"status": "fail", "tier0": "fail",
                             "tier1": "not-run", "tier2": "skipped",
                             "summary": "red as expected"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="verifier-driven actor passthrough")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    result = cond.gate_decide(
        t.id, action="approve",
        reason="test: verifier-driven actor passthrough; pytest -q -> 1 failed",
        actor=ADJUDICATOR_SEAT,
    )

    assert result["ok"] is True
    last = _last_gate_row(task_svc, t.id)
    assert last.actor == ADJUDICATOR_SEAT, (
        f"expected persisted actor {ADJUDICATOR_SEAT!r}, got {last.actor!r}")

    from prism_service.services.actor_service import ActorService
    from prism_service.models.actor import ActorKind
    resolved = ActorService().resolve(last.actor)
    assert resolved.kind is ActorKind.MACHINE, (
        f"expected {last.actor!r} to resolve MACHINE, got {resolved.kind!r}")


def test_verifier_driven_approve_persists_distinct_human_actor_verbatim(tmp_path):
    """A real human identity (the SPA Approve button's actor) must be
    persisted verbatim — not collapsed to the literal 'conductor'."""
    verifier = FakeVerifier({"status": "fail", "tier0": "fail",
                             "tier1": "not-run", "tier2": "skipped",
                             "summary": "red as expected"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="verifier-driven human actor passthrough")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    human_actor = "human:siegeon@gmail.com"
    result = cond.gate_decide(
        t.id, action="approve",
        reason="test: verifier-driven human actor passthrough; "
               "pytest -q -> 1 failed",
        actor=human_actor,
    )

    assert result["ok"] is True
    last = _last_gate_row(task_svc, t.id)
    assert last.actor == human_actor, (
        f"expected persisted actor {human_actor!r}, got {last.actor!r}")

    from prism_service.services.actor_service import ActorService
    from prism_service.models.actor import ActorKind
    resolved = ActorService().resolve(last.actor)
    assert resolved.kind is not ActorKind.MACHINE, (
        f"a human actor string must not resolve MACHINE, got {resolved.kind!r}")


def test_session_id_is_the_fallback_decider_when_no_actor_given(tmp_path):
    """No `actor`, but a `session_id` is supplied: the persisted actor
    falls back to the session id — never the literal 'conductor'."""
    verifier = FakeVerifier({"status": "fail", "tier0": "fail",
                             "tier1": "not-run", "tier2": "skipped",
                             "summary": "red as expected"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="session_id fallback decider")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    result = cond.gate_decide(
        t.id, action="approve",
        reason="test: session_id fallback decider; pytest -q -> 1 failed",
        session_id="sess-distinct-abc",
    )

    assert result["ok"] is True
    last = _last_gate_row(task_svc, t.id)
    assert last.actor == "sess-distinct-abc", (
        f"expected session_id fallback 'sess-distinct-abc', got {last.actor!r}")


# ----------------------------------------------------------------------
# Reject path (conductor_service.py:3164-3168)
# ----------------------------------------------------------------------


def test_reject_is_attributed_to_the_rejecting_actor(tmp_path):
    """A reject decision must also carry the real rejecting actor — not
    just approve paths."""
    verifier = FakeVerifier({"status": "pass", "tier0": "pass",
                             "tier1": "pass", "tier2": "skipped",
                             "summary": "unexpectedly green"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="reject actor passthrough")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    reviewer_actor = "human:reviewer@example.com"
    result = cond.gate_decide(
        t.id, action="reject", reason="design flaw", actor=reviewer_actor,
    )

    assert result["ok"] is True
    last = _last_gate_row(task_svc, t.id)
    assert last.actor == reviewer_actor, (
        f"expected persisted actor {reviewer_actor!r}, got {last.actor!r}")


# ----------------------------------------------------------------------
# Override path (conductor_service.py:3498)
# ----------------------------------------------------------------------


def test_override_approve_persists_caller_actor_not_manual_override_literal(
        tmp_path):
    """override=True with an explicit actor must persist THAT actor — the
    'manual-override' literal is a fallback for when no actor/session_id is
    supplied, never a mask over a real one (see companion test below for
    the fallback case, which must keep working)."""
    verifier = FakeVerifier({"status": "pass", "tier0": "pass",
                             "tier1": "pass", "tier2": "skipped",
                             "summary": "would have failed red gate"})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="override actor passthrough")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    result = cond.gate_decide(
        t.id, action="approve",
        reason="qa says it's fine; independent re-run: pytest -q -> 1 failed",
        override=True, actor=ADJUDICATOR_SEAT,
    )

    assert result["ok"] is True
    last = _last_gate_row(task_svc, t.id)
    assert last.actor == ADJUDICATOR_SEAT, (
        f"expected persisted actor {ADJUDICATOR_SEAT!r}, got {last.actor!r}")


def test_override_approve_without_actor_still_falls_back_to_manual_override(
        tmp_path):
    """Backward-compat guard: when no actor/session_id is supplied at all,
    the override path still stamps the 'manual-override' literal (this is
    the existing, still-green
    test_override_history_row_is_tagged_manual_override contract in
    test_conductor_v2_verifier_gate.py) — the fix must not regress it."""
    verifier = FakeVerifier({"status": "pass", "tier0": "pass",
                             "tier1": "pass", "tier2": "skipped",
                             "summary": ""})
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="override fallback unchanged")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    cond.gate_decide(
        t.id, action="approve",
        reason="urgent ship; independent re-run: pytest -> 1 failed",
        override=True,
    )

    last = _last_gate_row(task_svc, t.id)
    assert last.actor == "manual-override"


# ----------------------------------------------------------------------
# Epic roll-up path (conductor_service.py:3511)
# ----------------------------------------------------------------------


def test_epic_rollup_approve_persists_caller_actor_not_conductor(tmp_path):
    """A parent whose children all carry passing completion_proof
    satisfies its OWN green_gate via the roll-up path (issue #171). The
    distinct actor who clicked/called approve on the epic must be
    persisted — not the literal 'conductor'."""
    task_svc, cond = _services(tmp_path, verifier=None)
    parent = task_svc.create(title="epic rollup actor passthrough")
    for i in range(2):
        child = task_svc.create(
            title=f"epic child {i}", parent_id=parent.id)
        task_svc.update(
            child.id, status="done",
            completion_proof=f"child {i}: pytest -q -> 5 passed, 0 failed")

    _walk_to_gate(cond, parent.id, _gate_id("green_gate"))

    reviewer_actor = "human:reviewer@example.com"
    result = cond.gate_decide(
        parent.id, action="approve",
        reason="epic roll-up: all children done with strong proof",
        actor=reviewer_actor,
    )

    assert result["ok"] is True, result
    last = _last_gate_row(task_svc, parent.id)
    assert last.actor == reviewer_actor, (
        f"expected persisted actor {reviewer_actor!r}, got {last.actor!r}")
