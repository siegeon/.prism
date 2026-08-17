"""The owner's plain approve finishes an epic gate (task dbfe3727).

Live incident 2026-08-17, epic 37c9207b: the rollup park tooth (task
457b38db) refused the owner's own Approve click with "Approve as the
reviewing owner" - advice a plain approve could never satisfy, because the
tooth never consulted WHO was approving. It also fired for proof_type=test
(machine-graded) epics, over-parking the legitimate rollup path (epic suite
test_epic_rollup_never_closes_demo_gate.py AC-3, red on main since #2143).

Pins:
  AC-1  a plain approve on a demo epic with a green rollup, whose actor
        RESOLVES TO A HUMAN (the ActorService join the API route feeds
        since 98d38111), decides the gate: ok=True, gate_state=passed.
  AC-2  the identical call whose actor does NOT resolve human (agent
        session string) parks pending with a non-empty reason - 457b38db's
        protection intact. Resolution failure fails closed (also parks).
  AC-3  a proof_type=test epic with a green rollup closes via rollup with
        no human actor - the machine-graded path restored.
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
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


_STRONG_CHILD_PROOF = ("pytest tests/unit/test_x.py -q -> 5 passed, 0 "
                       "failed; all green")


def _parent(task_svc, title: str, proof_type: str):
    t = task_svc.create(title=title, tags=["conductor"],
                        oracle="a person reviews the demo end to end",
                        proof_type=proof_type,
                        likely_misfire="child status stands in for the "
                                      "owner's own review")
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending")
    return t


def _done_child(task_svc, parent_id: str, title: str):
    c = task_svc.create(title=title, parent_id=parent_id, oracle="x",
                        proof_type="test", likely_misfire="y",
                        completion_proof=_STRONG_CHILD_PROOF)
    task_svc.update(c.id, status="done")
    return c


def _human_actor(_raw):
    from prism_service.models.actor import Actor, ActorKind
    return Actor(id="user:local-user", kind=ActorKind.HUMAN,
                 display_name="siegeon", user_id="local-user")


# ---------------------------------------------------------------------------
# AC-1 -- a resolved-human plain approve decides a demo epic's gate
# ---------------------------------------------------------------------------


def test_owner_plain_approve_decides_demo_epic_gate(tmp_path, monkeypatch):
    from prism_service.services import conductor_service as cs
    task_svc, cond = _services(tmp_path)
    parent = _parent(task_svc, "epic the owner reviews", "demo")
    _done_child(task_svc, parent.id, "slice one")
    monkeypatch.setattr(cs, "_resolve_actor_identity", _human_actor)

    out = cond.gate_decide(parent.id, "approve",
                           reason="approved by owner (one-click)",
                           session_id="browser-session",
                           actor="siegeon@gmail.com")

    assert out["ok"] is True, out
    live = task_svc.get(parent.id)
    assert live.gate_state == "passed", (out, live.gate_state)


# ---------------------------------------------------------------------------
# AC-2 -- a non-human plain approve still parks (457b38db intact)
# ---------------------------------------------------------------------------


def test_agent_plain_approve_still_parks_demo_epic_gate(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _parent(task_svc, "epic an agent tries to close", "demo")
    _done_child(task_svc, parent.id, "slice one")

    out = cond.gate_decide(parent.id, "approve",
                           reason="Approving on live evidence: fresh "
                                  "passing oracle receipt (epic-rollup).",
                           session_id="distinct-drive-session",
                           actor="distinct-drive-session")

    assert out["ok"] is False, out
    assert str(out.get("reason", "")).strip(), out
    live = task_svc.get(parent.id)
    assert live.gate_state == "pending", (out, live.gate_state)


def test_resolution_failure_fails_closed_and_parks(tmp_path, monkeypatch):
    from prism_service.services import conductor_service as cs
    task_svc, cond = _services(tmp_path)
    parent = _parent(task_svc, "epic with a broken resolver", "demo")
    _done_child(task_svc, parent.id, "slice one")

    def _boom(_raw):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(cs, "_resolve_actor_identity", _boom)

    out = cond.gate_decide(parent.id, "approve", reason="click",
                           session_id="browser-session",
                           actor="siegeon@gmail.com")

    assert out["ok"] is False, out
    live = task_svc.get(parent.id)
    assert live.gate_state == "pending", (out, live.gate_state)


# ---------------------------------------------------------------------------
# AC-3 -- a test-proof epic still closes via rollup with no human
# ---------------------------------------------------------------------------


def test_test_proof_epic_rolls_up_without_a_human(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = _parent(task_svc, "machine-graded epic", "test")
    _done_child(task_svc, parent.id, "slice one")

    out = cond.gate_decide(parent.id, "approve",
                           reason="Approving on live evidence: fresh "
                                  "passing oracle receipt (epic-rollup).",
                           session_id="distinct-drive-session",
                           actor="distinct-drive-session")

    assert out["ok"] is True, out
    live = task_svc.get(parent.id)
    assert live.gate_state == "passed", (out, live.gate_state)
