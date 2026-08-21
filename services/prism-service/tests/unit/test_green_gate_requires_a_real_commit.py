"""green_gate must refuse a fresh, passing EvidenceReceipt when the task's
own workspace still has uncommitted changes (task 4f74dafc).

ROOT FAILURE this pins: oracle_spec.current_tree_sha is `git rev-parse HEAD`
-- the last REAL COMMIT -- but mint_green_evidence actually runs the pinned
tests against whatever is ON DISK in the workspace, committed or not. On
task 4f74dafc, HEAD was the tests-only red commit; the real implementation
sat as an uncommitted working-tree diff; the tests still passed (they read
the files on disk, not a git tree); the receipt recorded tree_sha=HEAD and
was genuinely "fresh" by the tree_sha+spec_hash match _oracle_receipt_
refusal already performs -- so the gate passed, status flipped to done,
and the actual implementation was never reachable from any commit at all.
Tests-green != committed. This suite pins the fix: a NEW mechanical tooth,
_uncommitted_changes_refusal, that ConductorService._oracle_receipt_refusal
now consults even after finding a fresh, passing receipt.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "app.py").write_text("def handler():\n    return 'old'\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "baseline")
    return r


def _svc():
    from prism_service.services.conductor_service import ConductorService

    svc = ConductorService.__new__(ConductorService)
    svc._project_name = "testproj"
    return svc


# ---------------------------------------------------------------------------
# The tooth in isolation: _uncommitted_changes_refusal
# ---------------------------------------------------------------------------

def test_clean_workspace_is_not_refused(tmp_path, monkeypatch):
    from prism_service.services import task_workspace

    repo = _repo(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    task = SimpleNamespace(id="t1", allowed_files=[])
    assert _svc()._uncommitted_changes_refusal(task) == ""


def test_dirty_workspace_is_refused(tmp_path, monkeypatch):
    from prism_service.services import task_workspace

    repo = _repo(tmp_path)
    (repo / "app.py").write_text("def handler():\n    return 'new'\n")
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    task = SimpleNamespace(id="t1", allowed_files=[])
    reason = _svc()._uncommitted_changes_refusal(task)
    assert reason, "an uncommitted change must be refused"
    assert "uncommitted" in reason.lower()
    assert "[task:<id8>]" in reason


def test_dirty_change_outside_allowed_files_does_not_block(tmp_path, monkeypatch):
    """A task scoped to allowed_files is only responsible for ITS OWN files
    -- an unrelated dirty file elsewhere in a shared checkout must not
    strand a compliant task's gate."""
    from prism_service.services import task_workspace

    repo = _repo(tmp_path)
    (repo / "unrelated.py").write_text("scratch\n")
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    task = SimpleNamespace(id="t1", allowed_files=["app.py"])
    assert _svc()._uncommitted_changes_refusal(task) == ""


def test_dirty_change_inside_allowed_files_blocks(tmp_path, monkeypatch):
    from prism_service.services import task_workspace

    repo = _repo(tmp_path)
    (repo / "app.py").write_text("def handler():\n    return 'new'\n")
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    task = SimpleNamespace(id="t1", allowed_files=["app.py"])
    reason = _svc()._uncommitted_changes_refusal(task)
    assert reason, "a dirty file inside allowed_files must be refused"


def test_no_workspace_is_not_refused(monkeypatch):
    """Abstain, don't refuse, when there's nothing to inspect -- same
    doctrine every neighbouring green_gate tooth follows (see
    test_green_gate_requires_reachability.py's identical case)."""
    from prism_service.services import task_workspace

    monkeypatch.setattr(task_workspace, "workspace_for", lambda _tid: None)
    task = SimpleNamespace(id="t1", allowed_files=[])
    assert _svc()._uncommitted_changes_refusal(task) == ""


# ---------------------------------------------------------------------------
# Integration: _oracle_receipt_refusal, the REAL function every green_gate
# call site consults, now refuses a fresh+passing receipt on a dirty tree --
# reproducing the exact 4f74dafc shape (tree_sha == HEAD, disk ahead of it).
# ---------------------------------------------------------------------------

def test_oracle_receipt_refusal_catches_a_fresh_receipt_on_a_dirty_tree(
        tmp_path, monkeypatch):
    from prism_service.services import task_workspace, oracle_spec as osp
    from prism_service.services import control_plane as cp

    repo = _repo(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    monkeypatch.setattr(osp, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(cp, "task_pin", lambda _tid: {"policy_hash": ""})

    task = SimpleNamespace(id="4f74dafc", oracle="", likely_misfire="",
                           allowed_files=[])
    spec = osp.OracleSpec.from_task(task)
    head = _git(repo, "rev-parse", "HEAD")

    receipt = osp.EvidenceReceipt(
        task_id=task.id, job_id="job-1", spec_hash=spec.spec_hash(),
        tree_sha=head, adapter="pytest_ids", passed=True, status=osp.ST_PASSED,
        reason="3 passed, 0 failed")
    osp.append_receipt("testproj", receipt)

    # Sanity: WITHOUT the new tooth (clean tree), this receipt IS fresh.
    assert osp.fresh_passing_receipt(
        "testproj", task.id, head, spec.spec_hash()) is not None

    # Now dirty the tree exactly like a driver that never committed its
    # implementation -- HEAD (and therefore tree_sha) is unchanged, but the
    # real content on disk has moved past what the receipt was measured on.
    (repo / "app.py").write_text("def handler():\n    return 'new'\n")

    svc = _svc()
    refusal, fresh_receipt = svc._oracle_receipt_refusal(
        task, override=False, reason="")
    assert refusal, (
        "a fresh+passing receipt on a DIRTY tree must be refused, not "
        "trusted at face value")
    assert fresh_receipt is None
    assert "uncommitted" in refusal.lower()


def test_oracle_receipt_refusal_still_passes_a_clean_fresh_receipt(
        tmp_path, monkeypatch):
    """Anti-over-strictness: the new tooth must not block the ORDINARY
    compliant case -- a fresh, passing receipt on a clean tree still
    clears the gate exactly as before."""
    from prism_service.services import task_workspace, oracle_spec as osp
    from prism_service.services import control_plane as cp

    repo = _repo(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    monkeypatch.setattr(cp, "task_pin", lambda _tid: {"policy_hash": ""})

    task = SimpleNamespace(id="clean-task", oracle="", likely_misfire="",
                           allowed_files=[])
    spec = osp.OracleSpec.from_task(task)
    head = _git(repo, "rev-parse", "HEAD")

    receipt = osp.EvidenceReceipt(
        task_id=task.id, job_id="job-1", spec_hash=spec.spec_hash(),
        tree_sha=head, adapter="pytest_ids", passed=True, status=osp.ST_PASSED,
        reason="3 passed, 0 failed")
    osp.append_receipt("testproj", receipt)

    svc = _svc()
    refusal, fresh_receipt = svc._oracle_receipt_refusal(
        task, override=False, reason="")
    assert refusal == "", (refusal, "a clean, fresh, passing receipt must "
                            "still clear the gate")
    assert fresh_receipt is not None
