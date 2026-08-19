"""The gate card stops promising what the approve path refuses — task e4e6cd44.

DEFECT: GET /api/conductor/gate/readiness returns `receipt_ok: true`,
adapter=human, "visual/demo gate — your review is the sign-off; Approve to
release" for a demo-proof task whose `[task:<id8>]` commits are NOT reachable
from origin/main — and then POST approve refuses that very click with the
unshipped banner. Audited 2026-08-18 on f506ece4, where the approve path
recorded 73 consecutive refusals while the card kept saying READY.

Root cause, verifiable by absence: `grep -n "_unshipped_gate_reason"
api/conductor.py` returns NOTHING. The tooth lives at
conductor_service.py `_unshipped_gate_reason` and ONLY the approve path
consults it; the readiness door's demo/human branch returns without ever
asking the shipped-ness question.

  AC-8a  shipper OFF (default) — readiness reports the unshipped refusal
         BEFORE any click, and the card renders the merge/land requirement.
         The click genuinely cannot succeed, so `receipt_ok` is honestly
         false and the Approve button is honestly disabled.
  AC-8b  shipper ON — the truth is disclosed WITHOUT disabling the button.
         LiveGatePanel.tsx disables Approve on `receipt_ok !== true`, so
         expressing the disclosure as a false receipt would disable the exact
         button task 5b6aefc1 exists to make work. The unshipped state is an
         ADVISORY line on an ENABLED card.
  AC-8c  anti-regression: once the trailer IS on origin/main, readiness flips
         back to the clean `your_review` shape with no disclosure noise.
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

# a completion_proof that satisfies the artifact tooth (test-runner signal +
# pass signal), so these tests exercise the SHIPPED-NESS question and not the
# visual-evidence one.
DEMO_PROOF = "pytest tests/unit/test_ship_worker.py -q -> 12 passed"


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
    """Actually land the branch on origin/main (AC-8c's precondition)."""
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


def _demo_task(task_svc, title="demo gate, unshipped"):
    t = task_svc.create(
        title=title, tags=["conductor"], proof_type="demo",
        oracle="Open the gate card and see the ship disclosure before clicking")
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending",
                    completion_proof=DEMO_PROOF)
    return t


def _reason(out: dict) -> str:
    return str((out.get("receipt") or {}).get("reason") or "") + " " + \
        str(out.get("receipt_refusal") or "")


# ---------------------------------------------------------------------------
# AC-8a — the pinned oracle: the card tells the truth BEFORE the click
# ---------------------------------------------------------------------------


def test_readiness_reports_unshipped_before_click(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi

    monkeypatch.delenv("PRISM_SHIP_ON_APPROVE", raising=False)
    task_svc, cond = _services(tmp_path)
    t = _demo_task(task_svc)
    origin, work, branch = _unshipped_repo(tmp_path, t.id)
    _wire(monkeypatch, tmp_path, cond, capi, work, branch, t.id)

    # the tooth itself objects — the premise of the whole defect
    assert cond._unshipped_gate_reason(task_svc.get(t.id)), (
        "fixture must be genuinely unshipped for this test to mean anything")

    out = capi.gate_readiness(task_id=t.id, project="default")

    assert out["receipt_ok"] is False, (
        "the card must NOT say 'Approve to release' for a click the approve "
        f"path will refuse. got: {out}")
    r = _reason(out)
    assert "origin/main" in r, r
    assert f"[task:{t.id[:8]}]" in r, r
    assert "Approve to release" not in r, r


def test_blocked_reason_carries_the_actionable_truth(tmp_path, monkeypatch):
    """The actionable text must outlive an oscillating gate_reason: the park
    path writes it to blocked_reason too (e4e6cd44's second half)."""
    from prism_service.api import conductor as capi

    task_svc, cond = _services(tmp_path)
    t = _demo_task(task_svc)
    origin, work, branch = _unshipped_repo(tmp_path, t.id)
    _wire(monkeypatch, tmp_path, cond, capi, work, branch, t.id)

    cond._park_green_refusal(t.id, cond._unshipped_gate_reason(task_svc.get(t.id)))

    live = task_svc.get(t.id)
    assert "origin/main" in str(getattr(live, "blocked_reason", "") or ""), (
        "blocked_reason must carry the merge/land requirement; gate_reason "
        "alone oscillates and the owner loses the actionable half")


# ---------------------------------------------------------------------------
# AC-8b — disclosure WITHOUT disabling the button the ticket exists to fix
# ---------------------------------------------------------------------------


def test_shipper_on_discloses_without_disabling_approve(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi

    monkeypatch.setenv("PRISM_SHIP_ON_APPROVE", "1")
    task_svc, cond = _services(tmp_path)
    t = _demo_task(task_svc)
    origin, work, branch = _unshipped_repo(tmp_path, t.id)
    _wire(monkeypatch, tmp_path, cond, capi, work, branch, t.id)

    out = capi.gate_readiness(task_id=t.id, project="default")

    assert out["receipt_ok"] is True, (
        "LiveGatePanel disables Approve on receipt_ok !== true — a false "
        "receipt here would disable the very button this feature adds. "
        f"got: {out}")
    assert out.get("unshipped") is True, out
    assert out.get("ship_on_approve") is True, out
    r = _reason(out).lower()
    assert "merge" in r and "pr" in r, (
        "the advisory must tell the owner what Approve is about to DO "
        f"(push, open a PR, wait for CI, merge). got: {r!r}")


# ---------------------------------------------------------------------------
# AC-8c — anti-regression: a genuinely shipped task reads clean
# ---------------------------------------------------------------------------


def test_shipped_task_reads_clean_your_review(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi

    monkeypatch.delenv("PRISM_SHIP_ON_APPROVE", raising=False)
    task_svc, cond = _services(tmp_path)
    t = _demo_task(task_svc, title="demo gate, already landed")
    origin, work, branch = _unshipped_repo(tmp_path, t.id)
    _land(work, branch)
    _wire(monkeypatch, tmp_path, cond, capi, work, branch, t.id)

    assert not cond._unshipped_gate_reason(task_svc.get(t.id)), (
        "fixture must be genuinely shipped")

    out = capi.gate_readiness(task_id=t.id, project="default")
    assert out["receipt_ok"] is True, out
    assert not out.get("unshipped"), out
    assert "origin/main" not in _reason(out), (
        "a landed task must carry no unshipped noise at all")
