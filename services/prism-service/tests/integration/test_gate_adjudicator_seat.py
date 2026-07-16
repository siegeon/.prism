"""Machine adjudicator seat pins — task 1d3322a6 (owner 2026-07-15).

The conductor decides its own green_gate as ``conductor-adjudicator`` when —
and only when — a FRESH PASSING EvidenceReceipt exists for the task's
current tree+spec+policy pin:

  AC-1  fresh passing receipt -> the pending green_gate is APPROVED by the
        adjudicator seat and the task advances past the gate.
  AC-2  manual_evidence_required oracle (browser floor) -> the gate stays
        PENDING for a human; the adjudicator never approves and never
        flips the gate to failed.
  AC-3  a receipt already exists for the CURRENT tree+spec but it FAILED ->
        one-attempt guard: the adjudicator does NOT re-run the oracle
        (no mint loop) and the gate stays pending.
  AC-4  unevidenced but machine-runnable oracle -> the adjudicator runs the
        oracle ONCE itself (mint) and approves on the fresh pass.
"""

from __future__ import annotations

import subprocess

import pytest


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True)


def _tiny_policy_repo(tmp_path):
    from prism_service.services import control_plane as cp
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    for pf in cp.POLICY_FILES:
        p = repo / pf
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("policy-v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    return repo, sha


@pytest.fixture()
def pinned_world(tmp_path, monkeypatch):
    import prism_service.services.task_workspace as tw
    from prism_service.services import control_plane as cp

    repo, sha = _tiny_policy_repo(tmp_path)
    ws = {"path": str(repo), "baseline": sha, "repo_root": str(repo)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: ws)
    monkeypatch.setattr(cp, "_workspace_for", lambda tid: ws)
    monkeypatch.delenv("PRISM_CONTROL_REF", raising=False)
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    return repo, sha


def _gated_task(tmp_path, oracle, proof_type, verify):
    """A task parked PENDING at green_gate with the given oracle shape."""
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="adjudicate me", oracle=oracle,
                        proof_type=proof_type)
    task_svc.update(t.id, verify=verify, workflow_step="green_gate",
                    gate_state="pending")
    return task_svc, task_svc.get(t.id)


def _conductor(tmp_path, task_svc):
    from prism_service.services.conductor_service import ConductorService
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = "testproj"
    return cond


def _stub_pytest_pass(monkeypatch):
    from prism_service.services import oracle_spec as osp
    monkeypatch.setitem(
        osp._ADAPTERS, osp.ADAPTER_PYTEST,
        lambda spec, ctx: ([], [], True, osp.ST_PASSED, "stub pass"))


# ---------------------------------------------------------------------------
# AC-1 — fresh passing receipt -> adjudicator approves, task advances
# ---------------------------------------------------------------------------


def test_fresh_receipt_is_approved_by_adjudicator(pinned_world, tmp_path,
                                                  monkeypatch):
    from prism_service.services import oracle_spec as osp
    from prism_service.services.conductor_service import ADJUDICATOR_SEAT
    repo, _ = pinned_world
    _stub_pytest_pass(monkeypatch)

    task_svc, task = _gated_task(
        tmp_path, oracle="pinned tests green", proof_type="test",
        verify=["pytest tests/unit/test_ok.py::test_ok"])
    spec = osp.OracleSpec.from_task(task)
    receipt = osp.run_oracle(
        spec, task, ctx={"project": "testproj", "workspace": str(repo)})
    assert receipt.passed is True

    cond = _conductor(tmp_path, task_svc)
    res = cond.adjudicate_green_gate(task.id)
    assert res is not None and res.get("ok") is True, res
    after = task_svc.get(task.id)
    assert after.gate_state == "passed"
    # green_gate is the terminal step: passing it completes the task.
    assert after.status == "done"
    # the decision reason carries the seat's receipt citation
    hist = task_svc.history(task.id)
    joined = " ".join(str(h) for h in hist)
    assert ADJUDICATOR_SEAT in joined or "machine adjudication" in joined


# ---------------------------------------------------------------------------
# AC-2 — manual-evidence oracle stays with a human, still pending
# ---------------------------------------------------------------------------


def test_manual_evidence_oracle_stays_pending(pinned_world, tmp_path):
    task_svc, task = _gated_task(
        tmp_path, oracle="the customer can read the page",
        proof_type="demo", verify=[])
    cond = _conductor(tmp_path, task_svc)
    res = cond.adjudicate_green_gate(task.id)
    assert res is None
    after = task_svc.get(task.id)
    assert after.gate_state == "pending"
    assert after.workflow_step == "green_gate"


# ---------------------------------------------------------------------------
# AC-3 — tried-and-FAILED evidence is never re-run (no mint loop)
# ---------------------------------------------------------------------------


def test_failed_receipt_is_not_retried(pinned_world, tmp_path, monkeypatch):
    from prism_service.services import oracle_spec as osp
    repo, _ = pinned_world
    task_svc, task = _gated_task(
        tmp_path, oracle="pinned tests green", proof_type="test",
        verify=["pytest tests/unit/test_ok.py::test_ok"])
    spec = osp.OracleSpec.from_task(task)
    tree = osp.current_tree_sha(str(repo))
    osp.append_receipt("testproj", osp.EvidenceReceipt(
        task_id=task.id, job_id="failed-1", spec_hash=spec.spec_hash(),
        tree_sha=tree, adapter=spec.adapter, passed=False, status="failed",
        policy_hash=""))

    cond = _conductor(tmp_path, task_svc)
    minted = {"called": False}
    monkeypatch.setattr(
        cond, "mint_green_evidence",
        lambda *a, **k: minted.__setitem__("called", True))
    res = cond.adjudicate_green_gate(task.id)
    assert res is None
    assert minted["called"] is False, "one-attempt guard must block re-mint"
    assert task_svc.get(task.id).gate_state == "pending"


# ---------------------------------------------------------------------------
# AC-4 — unevidenced machine-runnable oracle: mint ONCE, then approve
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# task 59ddfcbc — demo-proof red_gate: machine rubric approves; test-proof
# red_gate stays with the verifier path untouched
# ---------------------------------------------------------------------------


def test_demo_red_gate_is_approved_by_adjudicator(pinned_world, tmp_path):
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="demo ticket", oracle="the page shows X",
                        proof_type="demo")
    task_svc.update(t.id, workflow_step="red_gate", gate_state="pending")
    cond = _conductor(tmp_path, task_svc)
    res = cond.adjudicate_demo_red_gate(t.id)
    assert res is not None and res.get("ok") is True, res
    after = task_svc.get(t.id)
    # mid-flow gate: approve auto-advances to the next (non-gate) step,
    # which resets gate_state to 'none' — the advance IS the pass signal.
    assert after.workflow_step != "red_gate"
    assert after.gate_state != "failed"


def test_test_proof_red_gate_is_not_touched(pinned_world, tmp_path):
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="test ticket", oracle="pinned tests red",
                        proof_type="test")
    task_svc.update(t.id, workflow_step="red_gate", gate_state="pending")
    cond = _conductor(tmp_path, task_svc)
    res = cond.adjudicate_demo_red_gate(t.id)
    assert res is None
    assert task_svc.get(t.id).gate_state == "pending"


# ---------------------------------------------------------------------------
# 2026-07-16 defect pins — the judge seat is not a producer, and a machine
# refusal artifact is re-presentable while a human reject is final
# ---------------------------------------------------------------------------


def test_machine_seat_is_never_stamped_as_producer(pinned_world, tmp_path):
    from prism_service.services.conductor_service import ADJUDICATOR_SEAT
    task_svc, task = _gated_task(
        tmp_path, oracle="the page shows X", proof_type="demo", verify=[])
    task_svc.update(task.id, workflow_step="red_gate", gate_state="pending")
    cond = _conductor(tmp_path, task_svc)
    res = cond.adjudicate_demo_red_gate(task.id)
    assert res is not None and res.get("ok") is True, res
    stamped = [s.get("session_id")
               for s in task_svc.sessions_for_task(task.id)]
    assert ADJUDICATOR_SEAT not in stamped, stamped


def test_refused_approve_failure_is_re_presented(pinned_world, tmp_path,
                                                 monkeypatch):
    from prism_service.services import oracle_spec as osp
    repo, _ = pinned_world
    _stub_pytest_pass(monkeypatch)
    task_svc, task = _gated_task(
        tmp_path, oracle="pinned tests green", proof_type="test",
        verify=["pytest tests/unit/test_ok.py::test_ok"])
    spec = osp.OracleSpec.from_task(task)
    osp.run_oracle(spec, task,
                   ctx={"project": "testproj", "workspace": str(repo)})
    task_svc.update(task.id, gate_state="failed",
                    gate_reason="same-actor artifact")
    task_svc.record_history(
        task.id, action="gate_decide",
        details="gate=green_gate; action=approve; same-actor=rejected; "
                "actor=conductor-adjudicator", actor="conductor")
    cond = _conductor(tmp_path, task_svc)
    res = cond.adjudicate_green_gate(task.id)
    assert res is not None and res.get("ok") is True, res
    assert task_svc.get(task.id).gate_state == "passed"


def test_human_reject_is_final_for_the_seat(pinned_world, tmp_path,
                                            monkeypatch):
    from prism_service.services import oracle_spec as osp
    repo, _ = pinned_world
    _stub_pytest_pass(monkeypatch)
    task_svc, task = _gated_task(
        tmp_path, oracle="pinned tests green", proof_type="test",
        verify=["pytest tests/unit/test_ok.py::test_ok"])
    spec = osp.OracleSpec.from_task(task)
    osp.run_oracle(spec, task,
                   ctx={"project": "testproj", "workspace": str(repo)})
    task_svc.update(task.id, gate_state="failed",
                    gate_reason="not good enough")
    task_svc.record_history(
        task.id, action="gate_decide",
        details="gate=green_gate; action=reject; reason=not good enough",
        actor="conductor")
    cond = _conductor(tmp_path, task_svc)
    assert cond.adjudicate_green_gate(task.id) is None
    assert task_svc.get(task.id).gate_state == "failed"


def test_cancelled_task_is_never_adjudicated(pinned_world, tmp_path):
    task_svc, task = _gated_task(
        tmp_path, oracle="the page shows X", proof_type="demo", verify=[])
    task_svc.update(task.id, workflow_step="red_gate",
                    gate_state="pending", status="cancelled")
    cond = _conductor(tmp_path, task_svc)
    assert cond.adjudicate_demo_red_gate(task.id) is None


def test_unevidenced_oracle_is_minted_then_approved(pinned_world, tmp_path,
                                                    monkeypatch):
    from prism_service.services import oracle_spec as osp
    repo, _ = pinned_world
    _stub_pytest_pass(monkeypatch)
    task_svc, task = _gated_task(
        tmp_path, oracle="pinned tests green", proof_type="test",
        verify=["pytest tests/unit/test_ok.py::test_ok"])
    cond = _conductor(tmp_path, task_svc)

    def _mint(task_id, session_id=None, model=None, release=False):
        t = task_svc.get(task_id)
        spec = osp.OracleSpec.from_task(t)
        osp.run_oracle(spec, t, ctx={"project": "testproj",
                                     "workspace": str(repo)})
        return {"ok": True}

    monkeypatch.setattr(cond, "mint_green_evidence", _mint)
    res = cond.adjudicate_green_gate(task.id)
    assert res is not None and res.get("ok") is True, res
    assert task_svc.get(task.id).gate_state == "passed"
