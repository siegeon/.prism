"""The unshipped disclosure is proof_type-blind — task 8a06e121.

DEFECT: GET /api/conductor/gate/readiness only asks the shipped-ness
question (`ConductorService._unshipped_gate_reason`) inside the
HUMAN-JUDGMENT branch (api/conductor.py ~536-599), gated on
`pt in ("demo", "review") or osp.is_human_judgment(spec)`. A proof_type=test
task with a real pytest oracle falls straight through that `if` into the
generic EvidenceReceipt branch at the bottom of the function (api/
conductor.py ~602), which calls `s._oracle_receipt_refusal(...)` and NEVER
consults `_unshipped_gate_reason` at all.

gate_decide's approve path has NO such gap — conductor_service.py's
green_gate pre-flight calls `_unshipped_gate_reason` unconditionally for
EVERY proof_type before it ever reaches the oracle-receipt check. So a
proof_type=test task whose pinned tests genuinely pass, but whose own
`[task:<id8>]` commit has not reached origin/main, reads `receipt_ok: True`
("Approve" looks safe) from readiness while the very next Approve click is
refused by the same unshipped tooth readiness never asked — the identical
"Never show a human an Approve that dead-ends" shape task e4e6cd44 already
fixed for demo/review, reopened for the machine-graded lane.

Unlike the demo/review fix (task 5b6aefc1's AC-8b), a machine-graded gate
must NEVER gain a ship-on-approve escape (task contract stop_if): the
adjudicator owns proof_type=test, so this stays a FLAT honest refusal in
every environment, `PRISM_SHIP_ON_APPROVE` set or not.

  AC-1  unshipped + PRISM_SHIP_ON_APPROVE unset — readiness refuses BEFORE
        the click (receipt_ok False), naming origin/main and the trailer.
  AC-2  unshipped + PRISM_SHIP_ON_APPROVE=1 — STILL a flat refusal; no
        `ship_on_approve` advisory-on-an-enabled-card escape for
        machine-graded gates.
  AC-3  anti-regression: a genuinely shipped proof_type=test task reads
        clean — no unshipped noise once the trailer is on origin/main.
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


def _git(cwd, *args) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _clean_daemon_repo(tmp: Path) -> Path:
    repo = tmp / "daemon"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _write(repo, "README.md", "# ok\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _unshipped_repo(tmp_path: Path, task_id: str):
    """Real bare origin + a checkout carrying an UNPUSHED [task:id8] commit on
    the task's own branch — the live shape the tooth refuses on."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _write(work, "README.md", "# baseline\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "baseline")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "origin", "main")

    branch = f"prism/ws/{task_id}"
    _git(work, "checkout", "-q", "-b", branch)
    _write(work, "feature.py", "X = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", f"feat: the change\n\n[task:{task_id[:8]}]")
    return origin, work, branch


def _land(work: Path, branch: str) -> None:
    _git(work, "checkout", "-q", "main")
    _git(work, "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch)
    _git(work, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")


def _services(tmp_path):
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


class _FakeReceipt:
    """A genuinely PASSING EvidenceReceipt — the pinned suite is green. Used
    to isolate the tooth under test: the ONLY thing wrong with this task is
    that it has not shipped, never that its evidence is missing or stale."""
    adapter = "pytest"
    passed = True
    status = "passed"
    ended_at = "2026-08-19T00:00:00Z"
    reason = "3 passed"


def _wire(monkeypatch, tmp_path, cond, capi, work: Path, branch: str,
          task_id: str):
    daemon_root = tmp_path / "_daemon"
    daemon_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cp, "_prism_repo_root",
                        lambda: _clean_daemon_repo(daemon_root))
    import prism_service.services.task_workspace as tw
    rec = {"task_id": task_id, "path": str(work), "branch": branch,
           "repo_root": str(work)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: dict(rec))
    monkeypatch.setattr(capi, "_svc", lambda project: cond)
    # The oracle tooth itself is FINE — a fresh passing receipt on file — so
    # any refusal these tests observe can only be the unshipped tooth this
    # suite pins, never a red herring from an unrelated missing receipt.
    monkeypatch.setattr(
        type(cond), "_oracle_receipt_refusal",
        lambda self, task, **kw: ("", _FakeReceipt()))


def _test_proof_task(task_svc, title="machine-graded gate, unshipped"):
    t = task_svc.create(
        title=title, tags=["conductor"], proof_type="test",
        verify=["services/prism-service/tests/unit/test_feature.py"],
        oracle="pytest tests/unit/test_feature.py passes")
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending",
                    completion_proof="pytest -> 3 passed")
    return t


def _reason(out: dict) -> str:
    return str((out.get("receipt") or {}).get("reason") or "") + " " + \
        str(out.get("receipt_refusal") or "")


# ---------------------------------------------------------------------------
# AC-1 — the card tells the truth BEFORE the click, same as the demo lane
# ---------------------------------------------------------------------------


def test_readiness_reports_unshipped_for_a_test_proof_task(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi

    monkeypatch.delenv("PRISM_SHIP_ON_APPROVE", raising=False)
    task_svc, cond = _services(tmp_path)
    t = _test_proof_task(task_svc)
    origin, work, branch = _unshipped_repo(tmp_path, t.id)
    _wire(monkeypatch, tmp_path, cond, capi, work, branch, t.id)

    # the tooth itself objects — the premise of the whole defect
    assert cond._unshipped_gate_reason(task_svc.get(t.id)), (
        "fixture must be genuinely unshipped for this test to mean anything")

    out = capi.gate_readiness(task_id=t.id, project="default")

    assert out["receipt_ok"] is False, (
        "a machine-graded gate must not read 'passing' for a click the "
        f"approve path will refuse. got: {out}")
    r = _reason(out)
    assert "origin/main" in r, r
    assert f"[task:{t.id[:8]}]" in r, r


# ---------------------------------------------------------------------------
# AC-2 — no ship-on-approve escape for the machine-graded lane
# ---------------------------------------------------------------------------


def test_ship_on_approve_env_grants_no_escape_for_test_proof(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi

    monkeypatch.setenv("PRISM_SHIP_ON_APPROVE", "1")
    task_svc, cond = _services(tmp_path)
    t = _test_proof_task(task_svc)
    origin, work, branch = _unshipped_repo(tmp_path, t.id)
    _wire(monkeypatch, tmp_path, cond, capi, work, branch, t.id)

    out = capi.gate_readiness(task_id=t.id, project="default")

    assert out["receipt_ok"] is False, (
        "the ship seat is scoped to demo/review; a proof_type=test task must "
        f"stay a flat refusal even with the ship env on. got: {out}")
    assert not out.get("ship_on_approve"), (
        "machine-graded gates must never gain an advisory-on-an-enabled-"
        f"card escape hatch. got: {out}")


# ---------------------------------------------------------------------------
# AC-3 — anti-regression: a shipped test-proof task reads clean
# ---------------------------------------------------------------------------


def test_shipped_test_proof_task_reads_clean(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi

    monkeypatch.delenv("PRISM_SHIP_ON_APPROVE", raising=False)
    task_svc, cond = _services(tmp_path)
    t = _test_proof_task(task_svc, title="machine-graded gate, already landed")
    origin, work, branch = _unshipped_repo(tmp_path, t.id)
    _land(work, branch)
    _wire(monkeypatch, tmp_path, cond, capi, work, branch, t.id)

    assert not cond._unshipped_gate_reason(task_svc.get(t.id)), (
        "fixture must be genuinely shipped")

    out = capi.gate_readiness(task_id=t.id, project="default")
    assert out["receipt_ok"] is True, out
    assert "origin/main" not in _reason(out), (
        "a landed task must carry no unshipped noise at all")
