"""Gate decisions survive timeouts without double-advancing — red→green
pins for task b07fd46e-1492-4d70-b41d-2ad760e667bb.

The live defect (961f273b drive, memory mx-f8ed3f): gate_decide's
on-a-gate-step check runs at ENTRY, the verifier consult can take minutes,
and the passed-write + auto-advance happen afterwards with NO re-check —
so a timed-out client's retry lands a SECOND decision and the task
overshoots a step. And a passed-gate overshoot had NO repair path.

  AC-1  Two decisions racing on the SAME pending gate advance the task
        exactly ONCE; the loser is a recorded no-op naming the true step.
  AC-2  rewind_task moves an overshot task back exactly one step with a
        mandatory reason and a task_history audit row.
  AC-3  rewind is a guarded lever: blank reason refused, first step
        refused — never a silent hand-drive.
  AC-4  POST /api/conductor/rewind exposes the lever over HTTP.
"""

from __future__ import annotations


_PROOF = (
    "red: pytest -q — 4 failed with committed trace; green: full suite "
    "pytest tests -q 1459 passed; oracle walked on http://127.0.0.1:8888."
)


def _services(tmp_path):
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def _walk_to(task_svc, cond, task_id, step):
    """Advance a fresh task until it sits on ``step`` (gate pending)."""
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this idempotent-rewind walk (unrelated to premise content)
    # can leave review_previous_notes.
    task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising gate-decide idempotency "
        "and rewind, not a real premise claim - UNVERIFIED\n"))
    for _ in range(30):
        t = task_svc.get(task_id)
        if t.workflow_step == step:
            return t
        if t.gate_state == "pending":
            cond.gate_decide(task_id, "approve", reason="setup walk")
        else:
            cond.advance_task(task_id)
    raise AssertionError(f"never reached {step}")


def _history(task_svc, task_id):
    try:
        return task_svc.get_history(task_id)
    except AttributeError:
        return task_svc.history(task_id)


# ---------------------------------------------------------------------------
# AC-1 — exactly one advance under a decision race
# ---------------------------------------------------------------------------


def test_raced_gate_decisions_advance_exactly_once(tmp_path):
    """Deterministic race: the FIRST decision's verifier consult runs the
    SECOND decision inline before returning verified — exactly how a
    timed-out client's retry lands while the slow verifier is still going."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="race pin", oracle="x")
    task_svc.update(t.id, completion_proof=_PROOF)
    _walk_to(task_svc, cond, t.id, "red_gate")
    assert task_svc.get(t.id).gate_state == "pending"

    results: dict = {}
    calls = {"n": 0}

    def racing_verify(task, gate_step_id, proof_type=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # the retry lands ENTIRELY while this (slow) consult is running
            results["second"] = cond.gate_decide(
                t.id, "approve", reason="retry after client timeout")
        return {"verified": True, "reason": "stub verified",
                "verifier": {"ok": True}, "validation": "red_with_trace"}

    cond._verifier_svc = object()  # attach ANY verifier so the consult runs
    cond._verify_gate = racing_verify
    results["first"] = cond.gate_decide(
        t.id, "approve", reason="first decision, slow verifier")

    live = task_svc.get(t.id)
    # exactly ONE step past the gate — NOT two
    assert live.workflow_step == "implement_tasks", live.workflow_step
    oks = [k for k, v in results.items() if v.get("ok")]
    assert len(oks) == 1, results
    loser = results["second" if oks == ["first"] else "first"]
    assert loser.get("ok") is False
    assert "implement_tasks" in str(loser.get("reason", "")), loser
    # the losing decision is RECORDED, not silent
    rows = [r for r in _history(task_svc, t.id)
            if getattr(r, "action", "") == "gate_decide"
            and "no-op" in str(getattr(r, "details", ""))]
    assert rows, "raced decision left no audit row"


# ---------------------------------------------------------------------------
# AC-2 / AC-3 — audited rewind verb
# ---------------------------------------------------------------------------


def test_rewind_moves_back_one_step_with_audit(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="rewind pin", oracle="x")
    task_svc.update(t.id, completion_proof=_PROOF)
    _walk_to(task_svc, cond, t.id, "implement_tasks")

    res = cond.rewind_task(t.id, reason="overshoot repair (double-advance)",
                           actor="owner")
    assert res.get("ok") is True, res
    live = task_svc.get(t.id)
    assert live.workflow_step == "red_gate", live.workflow_step
    assert live.gate_state == "pending"  # target step is a gate
    rows = [r for r in _history(task_svc, t.id)
            if getattr(r, "action", "") == "rewind_task"]
    assert rows, "rewind left no audit row"
    detail = str(getattr(rows[-1], "details", ""))
    assert "overshoot repair" in detail
    assert getattr(rows[-1], "actor", "") == "owner"


def test_rewind_refuses_blank_reason_and_first_step(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="rewind guards", oracle="x")
    task_svc.update(t.id, completion_proof=_PROOF)
    _walk_to(task_svc, cond, t.id, "implement_tasks")

    before = task_svc.get(t.id).workflow_step
    res = cond.rewind_task(t.id, reason="   ")
    assert res.get("ok") is False
    assert task_svc.get(t.id).workflow_step == before
    assert not [r for r in _history(task_svc, t.id)
                if getattr(r, "action", "") == "rewind_task"]

    t2 = task_svc.create(title="first step", oracle="x")
    cond.advance_task(t2.id)  # enter the flow at its FIRST step
    first = task_svc.get(t2.id).workflow_step
    res2 = cond.rewind_task(t2.id, reason="cannot go below the floor")
    assert res2.get("ok") is False
    assert task_svc.get(t2.id).workflow_step == first


# ---------------------------------------------------------------------------
# AC-4 — the HTTP lever
# ---------------------------------------------------------------------------


def test_rewind_http_lever(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import conductor as conductor_api

    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="rewind http", oracle="x")
    task_svc.update(t.id, completion_proof=_PROOF)
    _walk_to(task_svc, cond, t.id, "implement_tasks")

    monkeypatch.setattr(conductor_api, "_svc", lambda project: cond)
    app = FastAPI()
    app.include_router(conductor_api.router, prefix="/api/conductor")
    client = TestClient(app)

    r = client.post("/api/conductor/rewind",
                    json={"task_id": t.id, "reason": "overshoot repair",
                          "actor": "owner"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    assert task_svc.get(t.id).workflow_step == "red_gate"

    r2 = client.post("/api/conductor/rewind",
                     json={"task_id": t.id, "reason": ""})
    assert r2.status_code >= 400 or r2.json().get("ok") is False
