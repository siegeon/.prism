"""A REFUSED approve parks the gate PENDING, never `failed` [task:97d92854].

Epic 37c9207b "Gates a human can always finish": when gate_decide refuses an
approve on oracle-receipt grounds (stale tree, receipt never minted), the
refusal is the SYSTEM saying "evidence not ready" — not a verdict on the work.
Writing gate_state="failed" strands the next honest approve behind
override=true. The fix mirrors the seat-side `_park_green_refusal` pattern:
gate_state stays "pending", the precise refusal reason lands in gate_reason,
the audit rows are kept, and NO receipt is minted by the refusal itself
(stop_if: override must not bypass the receipt check, and the approve handler
must never auto-mint evidence).

Fixture shape is the REAL stale-tree walk from
tests/integration/test_oracle_evidence_receipt.py::test_green_gate_refuses_stale_tree_sha
(receipt minted at commit A, workspace advanced to commit B), not a stub —
these tests pin the behaviour of the genuine `_oracle_receipt_refusal` path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import oracle_spec as osp  # noqa: E402

_MISFIRE = "the page crashes with a blank white screen"
_GREEN_REASON = ("verify_green: pytest full suite passed (0 failed); oracle "
                 "EvidenceReceipt http_probe passed against the surface")

_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _serve(status: int = 200, body: str = "<html>ok honest page</html>"):
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *a):
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/", srv


def _git_repo(tmp: Path) -> tuple[Path, str]:
    repo = tmp / "ws"
    repo.mkdir()
    e = {**os.environ, **_GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=e)
    (repo / "f.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=e)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=repo, check=True, env=e)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True, env=e).stdout.strip()
    return repo, sha


def _new_commit(repo: Path) -> str:
    e = {**os.environ, **_GIT_ENV}
    (repo / "f.txt").write_text("b", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "c2"], cwd=repo, check=True, env=e)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True, env=e).stdout.strip()


def _register_workspace(task_id: str, repo: Path) -> None:
    from prism_service.data_dir import resolve_data_dir
    root = resolve_data_dir() / "task_workspaces"
    root.mkdir(parents=True, exist_ok=True)
    idx_path = root / "index.json"
    idx = {}
    if idx_path.exists():
        idx = json.loads(idx_path.read_text())
    idx[task_id] = {"task_id": task_id, "path": str(repo),
                    "baseline": "", "branch": "b", "repo_root": str(repo)}
    idx_path.write_text(json.dumps(idx))


def _services(tmp_path):
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def _walk_to_green_gate(cond, task_svc, task_id: str) -> None:
    task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising refusal parking, not a real "
        "premise claim - UNVERIFIED\n"))
    guard = 40
    cleared = 0
    while guard > 0:
        guard -= 1
        snap = task_svc.get(task_id)
        if snap.workflow_step == "green_gate" and snap.gate_state == "pending":
            return
        if snap.gate_state == "pending":
            cleared += 1
            cond.gate_decide(
                task_id, action="approve",
                reason="walk intermediate; independent re-run: pytest -> 1 failed",
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}")
            continue
        cond.advance_task(task_id)
    raise AssertionError("never reached green_gate")


def _stale_receipt_task(tmp_path):
    """Task at green_gate with a PASSING receipt minted at commit A while the
    workspace sits at commit B — the canonical stale-tree refusal."""
    task_svc, cond = _services(tmp_path)
    repo, sha_a = _git_repo(tmp_path)
    url, srv = _serve(status=200, body="<html>ok honest page renders</html>")
    try:
        t = task_svc.create(title="refusal parks pending fixture",
                            tags=["conductor"], oracle=url,
                            likely_misfire=_MISFIRE, proof_type="test",
                            completion_proof=f"loaded {url}; docs/ok.png")
        _walk_to_green_gate(cond, task_svc, t.id)
        _register_workspace(t.id, repo)
        live = task_svc.get(t.id)
        r = osp.run_oracle(osp.OracleSpec.from_task(live), live,
                           ctx={"project": "default", "workspace": str(repo)})
        assert r.passed is True and r.tree_sha == sha_a
    finally:
        srv.shutdown()
    sha_b = _new_commit(repo)
    assert sha_b != sha_a
    return task_svc, cond, t


def test_plain_approve_on_stale_receipt_parks_pending_not_failed(tmp_path):
    task_svc, cond, t = _stale_receipt_task(tmp_path)
    before = len(osp.read_receipts("default", t.id))
    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    assert res["ok"] is False, res
    assert "stale" in res["reason"].lower()
    live = task_svc.get(t.id)
    # The refusal PARKS, it does not condemn: pending + actionable reason.
    assert res["gate_state"] == "pending", res
    assert live.gate_state == "pending", live.gate_state
    assert live.gate_reason and "stale" in live.gate_reason.lower()
    # The refusal itself minted nothing (no auto-evidence in the handler).
    assert len(osp.read_receipts("default", t.id)) == before


def test_override_approve_on_stale_receipt_parks_pending_not_failed(tmp_path):
    task_svc, cond, t = _stale_receipt_task(tmp_path)
    before = len(osp.read_receipts("default", t.id))
    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           override=True, actor="qa-final",
                           session_id="qa-final")
    # NO-OVERRIDE-SKIPS-THE-ORACLE holds: still refused...
    assert res["ok"] is False, res
    live = task_svc.get(t.id)
    # ...but refused into PENDING, never failed.
    assert res["gate_state"] == "pending", res
    assert live.gate_state == "pending", live.gate_state
    assert live.gate_reason
    assert len(osp.read_receipts("default", t.id)) == before


def test_refusal_leaves_receipt_count_unchanged(tmp_path):
    """AC-3 pin: back-to-back refused approves (plain then override) never
    mint evidence — receipt count is constant across both refusals."""
    task_svc, cond, t = _stale_receipt_task(tmp_path)
    before = len(osp.read_receipts("default", t.id))
    cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                     actor="qa-final", session_id="qa-final")
    cond.gate_decide(t.id, "approve", reason=_GREEN_REASON, override=True,
                     actor="qa-final", session_id="qa-final")
    assert len(osp.read_receipts("default", t.id)) == before
    assert task_svc.get(t.id).gate_state == "pending"
