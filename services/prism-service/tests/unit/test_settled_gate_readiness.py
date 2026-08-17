"""A finished task's gate stops contradicting itself - task 39adc067.

DEFECT: GET /api/conductor/gate/readiness (api/conductor.py, gate_readiness)
re-litigates tree-staleness for a gate that is ALREADY DECIDED
(task.gate_state == "passed"), producing the exact same "STALE ... re-run
the oracle" refusal text a genuinely unsound approval would produce
(e0149f1f's mirror-image bug) - a reader cannot tell a sound decided gate
from a false-green one. Repro subject: task 0a9b511f (done, green_gate,
gate_state=passed) - readiness returns receipt_ok:false with the STALE text
even though the gate was already approved on a fresh receipt at decision
time (verified live against the running dev daemon on 2026-08-17).

  AC-1  DONE task, workflow_step=green_gate, gate_state=passed, gate_reason
        carries "tree=<sha>" (the machine-adjudication format stamped at
        conductor_service.py gate_decide) -> receipt_ok:true, the receipt
        reason states the gate was DECIDED and names the tree it was
        decided at, and NEVER contains the STALE refusal text.
  AC-2  the current tree differs from the decided-at tree on a settled
        (gate_state=passed) task -> the drift is appended as INFORMATIONAL
        context in the reason string, receipt_ok stays true (never false).
  AC-3  anti-regression: an otherwise-identical task whose gate is still
        PENDING (gate_state != "passed") with a genuinely stale/foreign-tree
        receipt still returns receipt_ok:false with the EXISTING STALE
        refusal text unchanged verbatim - the e0149f1f false-green catch
        must survive this fix untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import control_plane as cp  # noqa: E402


def _git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _clean_daemon_repo(tmp: Path) -> Path:
    """Disposable clean repo standing in for the daemon's OWN checkout, so
    the dirty-judge check (runs FIRST, ahead of every branch) never trips
    on the real E:\\.prism working tree's unrelated dirt."""
    repo = tmp / "daemon"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "README.md", "# ok\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _task_repo(tmp: Path, name: str) -> tuple[Path, str]:
    repo = tmp / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "some/file.py", "X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo, _git(repo, "rev-parse", "HEAD")


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def _wire(monkeypatch, tmp_path, cond, capi, ws_path):
    daemon_root = tmp_path / "_daemon"
    daemon_root.mkdir(parents=True, exist_ok=True)
    daemon = _clean_daemon_repo(daemon_root)
    monkeypatch.setattr(cp, "_prism_repo_root", lambda: daemon)
    import prism_service.services.task_workspace as tw
    monkeypatch.setattr(tw, "workspace_for",
                        lambda tid: {"path": str(ws_path)})
    monkeypatch.setattr(capi, "_svc", lambda project: cond)


# ---------------------------------------------------------------------------
# AC-1 - settled gate, no drift: reads decided, never STALE
# ---------------------------------------------------------------------------


def test_settled_gate_reads_decided_not_stale(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    repo, head = _task_repo(tmp_path, "task_ws_1")
    _wire(monkeypatch, tmp_path, cond, capi, repo)

    t = task_svc.create(title="settled green-gate task", tags=["conductor"],
                        oracle="pytest tests/unit/test_x.py -q",
                        proof_type="test")
    reason = (f"verified (green_receipt): machine adjudication: fresh "
             f"passing EvidenceReceipt job-1 (adapter=pytest_ids, "
             f"tree={head[:12]}) \u2014 the server exercised the oracle and "
             "approves on the receipt tooth")
    task_svc.update(t.id, verify=["tests/unit/test_x.py"],
                    workflow_step="green_gate", gate_state="passed",
                    gate_reason=reason)

    out = capi.gate_readiness(task_id=t.id, project="default")
    assert out["receipt_ok"] is True, out
    r = out["receipt"]["reason"]
    assert "decided" in r.lower(), r
    assert head[:12] in r, r
    assert "STALE" not in r, r
    assert "re-run the oracle" not in r, r


# ---------------------------------------------------------------------------
# AC-2 - settled gate, tree HAS drifted: drift is informational, still ok
# ---------------------------------------------------------------------------


def test_settled_gate_tree_drift_is_informational_not_a_refusal(
        tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    repo, decided_head = _task_repo(tmp_path, "task_ws_2")
    _wire(monkeypatch, tmp_path, cond, capi, repo)

    t = task_svc.create(title="settled task, tree moved on",
                        tags=["conductor"],
                        oracle="pytest tests/unit/test_x.py -q",
                        proof_type="test")
    reason = (f"verified (green_receipt): machine adjudication: fresh "
             f"passing EvidenceReceipt job-2 (adapter=pytest_ids, "
             f"tree={decided_head[:12]}) \u2014 the server exercised the "
             "oracle")
    task_svc.update(t.id, verify=["tests/unit/test_x.py"],
                    workflow_step="green_gate", gate_state="passed",
                    gate_reason=reason)

    # a NEW commit lands in the workspace AFTER the decision - the settled
    # drift case AC-2 targets (e.g. a follow-up commit merged post-approval).
    _write(repo, "some/other.py", "Y = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "post-decision commit")
    new_head = _git(repo, "rev-parse", "HEAD")
    assert new_head != decided_head

    out = capi.gate_readiness(task_id=t.id, project="default")
    assert out["receipt_ok"] is True, out
    r = out["receipt"]["reason"]
    assert decided_head[:12] in r, r          # names the decided-at tree
    assert new_head[:12] in r, r              # names the drifted-to tree
    assert "STALE" not in r, r
    assert "re-run the oracle" not in r, r


# ---------------------------------------------------------------------------
# AC-3 - anti-regression: PENDING gate + foreign-tree receipt stays refused
# ---------------------------------------------------------------------------


def test_pending_gate_with_stale_receipt_still_refuses_verbatim(
        tmp_path, monkeypatch):
    """Same shape (green_gate, stale receipt) but gate_state is PENDING, not
    passed - the e0149f1f false-green catch must be untouched: this is the
    one lever this task's fix must NOT weaken (task stop_if)."""
    from prism_service.api import conductor as capi
    from prism_service.services import oracle_spec as osp
    task_svc, cond = _services(tmp_path)
    repo, head = _task_repo(tmp_path, "task_ws_3")
    _wire(monkeypatch, tmp_path, cond, capi, repo)

    t = task_svc.create(title="pending green-gate task, stale receipt",
                        tags=["conductor"],
                        oracle="pytest tests/unit/test_x.py -q",
                        proof_type="test")
    task_svc.update(t.id, verify=["tests/unit/test_x.py"],
                    workflow_step="green_gate", gate_state="pending")
    task = task_svc.get(t.id)
    spec = osp.OracleSpec.from_task(task)

    # a receipt on file from a PRIOR, foreign tree - never the current HEAD.
    osp.append_receipt("default", osp.EvidenceReceipt(
        task_id=t.id, job_id="job-3", spec_hash=spec.spec_hash(),
        tree_sha="0" * 40, adapter=spec.adapter, passed=True,
        status=osp.ST_PASSED, ended_at="2020-01-01T00:00:00Z",
        reason="stub"))

    out = capi.gate_readiness(task_id=t.id, project="default")
    assert out["receipt_ok"] is False, out
    refusal = out["receipt_refusal"]
    assert refusal.startswith(
        "green_gate: oracle not evidenced \u2014 latest passing receipt "
        "is STALE \u2014 it was run at tree="), refusal
    assert "re-run the oracle" in refusal, refusal
