"""Trusted green-gate oracle — OracleSpec + real runner + append-only
EvidenceReceipt (inverted-flow soundness #2).

The old green_gate oracle tooth (arc_governance.score_green_outcome) token-
matched the STORED completion_proof against words scraped from task.oracle —
a gameable, polarity-blind, fail-OPEN lint that validated PROSE SHAPE, not the
customer observable. These tests pin the replacement: the gate is claimable
ONLY when a FRESH PASSING EvidenceReceipt exists (its tree_sha matches the
task's current commit AND its spec_hash matches the task's current OracleSpec),
produced by a REAL run of the oracle's adapter. A failing run, a stale tree, a
stale spec, or a bare override with no receipt all REFUSE.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import types
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import oracle_spec as osp  # noqa: E402


# ---------------------------------------------------------------------------
# Harnesses
# ---------------------------------------------------------------------------


def _serve(status: int = 200, body: str = "<html>ok honest page</html>"):
    """Start a real local HTTP server returning a fixed status + body.
    Returns (base_url, server); caller shuts it down."""
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *a):  # silence
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/", srv


def _git_repo(tmp: Path) -> tuple[Path, str]:
    """A real one-commit git repo; returns (path, HEAD sha)."""
    repo = tmp / "ws"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    import os
    e = {**os.environ, **env}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=e)
    (repo / "f.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=e)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=repo, check=True, env=e)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True, env=e).stdout.strip()
    return repo, sha


def _new_commit(repo: Path) -> str:
    import os
    e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (repo / "f.txt").write_text("b", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "c2"], cwd=repo, check=True, env=e)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True, env=e).stdout.strip()


def _register_workspace(task_id: str, repo: Path) -> None:
    """Register a task workspace so the gate resolves the repo's HEAD as the
    task's current tree_sha (the freshness key)."""
    import json
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


_MISFIRE = "the page crashes with a blank white screen"


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def _walk_to_green_gate(cond, task_id: str) -> None:
    from prism_service.models.workflow import WORKFLOW_STEPS
    target = next(i for i, s in enumerate(WORKFLOW_STEPS)
                  if s["id"] == "green_gate")
    guard = (target + 1) * 3
    cleared = 0
    while guard > 0:
        guard -= 1
        snap = cond._task_svc.get(task_id)
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


def _make_task(task_svc, cond, oracle: str, **kw):
    t = task_svc.create(title=kw.pop("title", "oracle receipt fixture"),
                        tags=kw.pop("tags", ["conductor"]), oracle=oracle,
                        likely_misfire=kw.pop("likely_misfire", _MISFIRE),
                        proof_type=kw.pop("proof_type", "test"),
                        completion_proof=kw.pop("completion_proof", ""), **kw)
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this oracle-receipt walk (unrelated to premise content) can
    # leave review_previous_notes.
    task_svc.update(t.id, premise_notes=(
        "## Premises\n- fixture walk exercising the oracle EvidenceReceipt "
        "tooth, not a real premise claim - UNVERIFIED\n"))
    _walk_to_green_gate(cond, t.id)
    return t


# Reason that satisfies the (separate) proof-carrying artifact tooth so the
# ONLY thing under test on the pass path is the fresh-receipt oracle tooth.
_GREEN_REASON = ("verify_green: pytest full suite passed (0 failed); oracle "
                 "EvidenceReceipt http_probe passed against the surface")


# ---------------------------------------------------------------------------
# Adapter-level: http_probe polarity + pytest_ids + browser + append-only
# ---------------------------------------------------------------------------


def test_http_probe_negative_violation_receipt_fails():
    """A body carrying an error token (status<400) VIOLATES the negative
    assertion — the polarity fix: a broken 200 page is not a pass."""
    url, srv = _serve(status=200, body="Traceback (most recent call last): "
                                       "NameError: foo is not defined")
    try:
        task = types.SimpleNamespace(id=str(uuid.uuid4()), oracle=url,
                                     likely_misfire=_MISFIRE)
        spec = osp.OracleSpec.from_task(task)
        r = osp.run_oracle(spec, task, ctx={"project": "default"})
    finally:
        srv.shutdown()
    assert r.passed is False and r.status == osp.ST_FAILED
    assert any(o["name"] == "no_error_token" and not o["passed"]
               for o in r.observations)


def test_http_probe_status_500_receipt_fails():
    url, srv = _serve(status=500, body="server exploded")
    try:
        task = types.SimpleNamespace(id=str(uuid.uuid4()), oracle=url,
                                     likely_misfire=_MISFIRE)
        r = osp.run_oracle(osp.OracleSpec.from_task(task), task,
                           ctx={"project": "default"})
    finally:
        srv.shutdown()
    assert r.passed is False


def test_http_probe_passing_receipt_ok():
    url, srv = _serve(status=200, body="<html>ok honest page renders</html>")
    try:
        task = types.SimpleNamespace(id=str(uuid.uuid4()), oracle=url,
                                     likely_misfire=_MISFIRE)
        r = osp.run_oracle(osp.OracleSpec.from_task(task), task,
                           ctx={"project": "default"})
    finally:
        srv.shutdown()
    assert r.passed is True and r.status == osp.ST_PASSED


def test_pytest_ids_adapter_pass_and_fail(tmp_path):
    good = tmp_path / "sample_ok_test.py"
    good.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    bad = tmp_path / "sample_bad_test.py"
    bad.write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    task = types.SimpleNamespace(id=str(uuid.uuid4()), oracle="", likely_misfire="")

    ok_spec = osp.OracleSpec(adapter=osp.ADAPTER_PYTEST,
                             target=f"{good}::test_ok")
    r_ok = osp.run_oracle(ok_spec, task, ctx={
        "project": "default", "workspace": str(tmp_path),
        "pytest_python": sys.executable})
    assert r_ok.passed is True

    bad_spec = osp.OracleSpec(adapter=osp.ADAPTER_PYTEST,
                              target=f"{bad}::test_bad")
    r_bad = osp.run_oracle(bad_spec, task, ctx={
        "project": "default", "workspace": str(tmp_path),
        "pytest_python": sys.executable})
    assert r_bad.passed is False


def test_browser_adapter_manual_evidence_required():
    task = types.SimpleNamespace(id=str(uuid.uuid4()),
                                 oracle="you can SEE the dashboard render",
                                 likely_misfire="")
    spec = osp.OracleSpec.from_task(task)   # no URL -> browser adapter
    assert spec.adapter == osp.ADAPTER_BROWSER
    r = osp.run_oracle(spec, task, ctx={"project": "default"})
    assert r.status == osp.ST_MANUAL and r.passed is False


def test_receipts_are_append_only():
    tid = str(uuid.uuid4())
    task = types.SimpleNamespace(id=tid, oracle="see it", likely_misfire="")
    spec = osp.OracleSpec.from_task(task)
    r1 = osp.run_oracle(spec, task, ctx={"project": "default"})
    r2 = osp.run_oracle(spec, task, ctx={"project": "default"})
    rows = osp.read_receipts("default", tid)
    assert len(rows) == 2, "second run must APPEND, not overwrite"
    assert rows[0].job_id == r1.job_id and rows[1].job_id == r2.job_id


def test_spec_hash_stable_and_edit_sensitive():
    t1 = types.SimpleNamespace(oracle="http://127.0.0.1:9/x", likely_misfire="")
    t2 = types.SimpleNamespace(oracle="http://127.0.0.1:9/x", likely_misfire="")
    t3 = types.SimpleNamespace(oracle="http://127.0.0.1:9/y", likely_misfire="")
    assert osp.OracleSpec.from_task(t1).spec_hash() == \
        osp.OracleSpec.from_task(t2).spec_hash()
    assert osp.OracleSpec.from_task(t1).spec_hash() != \
        osp.OracleSpec.from_task(t3).spec_hash()


# ---------------------------------------------------------------------------
# Gate-level: fresh-receipt rule, drift invalidation, override closed
# ---------------------------------------------------------------------------


def test_green_gate_refuses_without_receipt(tmp_path):
    task_svc, cond = _services(tmp_path)
    url, srv = _serve()
    srv.shutdown()   # never run — no receipt on file
    t = _make_task(task_svc, cond, oracle=url,
                   completion_proof="all unit tests pass")
    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    # Superseded by task 97d92854: a receipt refusal PARKS the gate
    # pending (reason recorded, nothing minted) instead of writing "failed",
    # so the next honest approve needs no override escalation.
    assert res["ok"] is False and res["gate_state"] == "pending"
    assert "oracle" in res["reason"].lower()
    assert task_svc.get(t.id).gate_state == "pending"


def test_green_gate_refuses_on_failing_receipt(tmp_path):
    task_svc, cond = _services(tmp_path)
    url, srv = _serve(status=200, body="Traceback (most recent call last): boom")
    try:
        t = _make_task(task_svc, cond, oracle=url)
        live = task_svc.get(t.id)
        r = osp.run_oracle(osp.OracleSpec.from_task(live), live,
                           ctx={"project": "default"})
        assert r.passed is False
    finally:
        srv.shutdown()
    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    # Superseded by task 97d92854: refusals park pending, never failed.
    assert res["ok"] is False and res["gate_state"] == "pending"


def test_green_gate_claimable_on_fresh_passing_receipt(tmp_path):
    task_svc, cond = _services(tmp_path)
    url, srv = _serve(status=200, body="<html>ok honest page renders</html>")
    try:
        t = _make_task(task_svc, cond, oracle=url,
                       completion_proof=f"agent-browser loaded {url}; "
                                        "screenshot docs/ok.png")
        live = task_svc.get(t.id)
        r = osp.run_oracle(osp.OracleSpec.from_task(live), live,
                           ctx={"project": "default"})
        assert r.passed is True
    finally:
        srv.shutdown()
    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    assert res["ok"] is True, res
    assert task_svc.get(t.id).gate_state == "passed"


def test_green_gate_refuses_stale_tree_sha(tmp_path):
    """A passing receipt run at commit A is STALE once the workspace advances
    to commit B — the gate refuses until a fresh run at B lands."""
    task_svc, cond = _services(tmp_path)
    repo, sha_a = _git_repo(tmp_path)
    url, srv = _serve(status=200, body="<html>ok honest page renders</html>")
    try:
        t = _make_task(task_svc, cond, oracle=url,
                       completion_proof=f"loaded {url}; docs/ok.png")
        _register_workspace(t.id, repo)
        live = task_svc.get(t.id)
        r = osp.run_oracle(osp.OracleSpec.from_task(live), live,
                           ctx={"project": "default", "workspace": str(repo)})
        assert r.passed is True and r.tree_sha == sha_a
    finally:
        srv.shutdown()
    sha_b = _new_commit(repo)
    assert sha_b != sha_a
    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    # Superseded by task 97d92854: refusals park pending, never failed.
    assert res["ok"] is False and res["gate_state"] == "pending"
    assert "stale" in res["reason"].lower()


def test_green_gate_refuses_stale_spec_hash(tmp_path):
    """Editing the oracle after a run changes the spec_hash — the prior
    receipt no longer describes the current oracle, so it is STALE."""
    task_svc, cond = _services(tmp_path)
    url, srv = _serve(status=200, body="<html>ok honest page renders</html>")
    try:
        t = _make_task(task_svc, cond, oracle=url,
                       completion_proof=f"loaded {url}; docs/ok.png")
        live = task_svc.get(t.id)
        r = osp.run_oracle(osp.OracleSpec.from_task(live), live,
                           ctx={"project": "default"})
        assert r.passed is True
    finally:
        srv.shutdown()
    task_svc.update(t.id, oracle=url.rstrip("/") + "/edited")   # spec drift
    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    # Superseded by task 97d92854: refusals park pending, never failed.
    assert res["ok"] is False and res["gate_state"] == "pending"


def test_override_cannot_green_without_receipt(tmp_path):
    """The override path is no longer an oracle-skip hole: overriding a
    green_gate that DECLARES an oracle still requires a fresh passing receipt
    (or an explicit manual_evidence_required ack)."""
    task_svc, cond = _services(tmp_path)
    t = _make_task(task_svc, cond, oracle="http://127.0.0.1:9/never",
                   completion_proof="looks green to me")
    res = cond.gate_decide(t.id, "approve",
                           reason="independent review, overriding to green",
                           override=True, actor="qa-override",
                           session_id="qa-override")
    # Superseded by task 97d92854: refusals park pending, never failed.
    assert res["ok"] is False and res["gate_state"] == "pending"
    assert "oracle" in res["reason"].lower()
