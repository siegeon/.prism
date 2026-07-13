"""Pinned control-plane for gate policy (inverted-flow soundness #3).

THE DEFECT: the candidate under test supplied its OWN judge — the branch a
gate adjudicates contains the gate policy (governance rubrics, the arc-
governance scorer, the verifier ignore/lane rules, the oracle spec/thresholds,
the conductor gate logic). A task could loosen its own rubric/ignores to pass.

These tests pin the fix:
  * gate policy is LOADED from a PINNED control ref (git show <ref>:<path>
    against the MAIN checkout), never the candidate's worktree — so a worktree
    edit to the rubric does not change what the loader sees;
  * a gate REFUSES when the task's worktree diff modifies ANY enumerated
    policy file (the candidate-controls-judge tooth), unless the change is
    routed through an explicitly-authorized path (env flag / task tag);
  * control_ref + policy_hash are STAMPED on the EvidenceReceipt + the gate
    decision; a receipt minted under a different pinned policy_hash is stale;
  * the already-present guarantees still hold — distinct-actor refusal,
    append-only receipts, a session-less report rejected, and an un-runnable
    oracle yields manual_evidence_required, never a policy-pass.
"""

from __future__ import annotations

import os
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

from prism_service.services import control_plane as cp  # noqa: E402
from prism_service.services import oracle_spec as osp  # noqa: E402

_RUBRIC_REL = cp.POLICY_FILES[0]   # .../governance_rubrics.yaml
_NONPOLICY_REL = "services/prism-service/prism_service/services/task_service.py"
_PINNED_RUBRIC = "story_complete:\n  required_sections:\n    - Summary\n"
_LOOSENED_RUBRIC = "story_complete:\n  required_sections: []\n"
_MISFIRE = "the page crashes with a blank white screen"


# ---------------------------------------------------------------------------
# Real-git harness: a repo carrying policy files + a worktree the candidate
# edits, mirroring services/task_workspace.ensure_workspace's records.
# ---------------------------------------------------------------------------


def _git(cwd, *args):
    e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, env=e,
                          capture_output=True, text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _policy_repo(tmp: Path) -> tuple[Path, str, Path]:
    """A real repo carrying the rubric + a non-policy file at their true
    repo-relative paths; returns (repo, baseline_sha, worktree). The worktree
    is a branch off baseline — where the candidate's edits land."""
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, _RUBRIC_REL, _PINNED_RUBRIC)
    _write(repo, _NONPOLICY_REL, "# non-policy source\nX = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    wt = tmp / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "prism/ws/t", str(wt), baseline)
    return repo, baseline, wt


def _register_ws(task_id: str, wt: Path, baseline: str, repo: Path) -> None:
    import json
    from prism_service.data_dir import resolve_data_dir
    root = resolve_data_dir() / "task_workspaces"
    root.mkdir(parents=True, exist_ok=True)
    idx_path = root / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    idx[task_id] = {"task_id": task_id, "path": str(wt), "baseline": baseline,
                    "branch": "prism/ws/t", "repo_root": str(repo)}
    idx_path.write_text(json.dumps(idx))


def _serve(status: int = 200, body: str = "<html>ok honest page renders</html>"):
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


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def _advance_to(cond, task_id: str, gate_id: str) -> None:
    """Advance (no gates precede story_gate) until sitting on gate_id."""
    for _ in range(30):
        snap = cond._task_svc.get(task_id)
        if snap.workflow_step == gate_id and snap.gate_state == "pending":
            return
        cond.advance_task(task_id)
    raise AssertionError(f"never reached {gate_id}")


def _walk_to_green_gate(cond, task_id: str) -> None:
    from prism_service.models.workflow import WORKFLOW_STEPS
    target = next(i for i, s in enumerate(WORKFLOW_STEPS)
                  if s["id"] == "green_gate")
    guard, cleared = (target + 1) * 3, 0
    while guard > 0:
        guard -= 1
        snap = cond._task_svc.get(task_id)
        if snap.workflow_step == "green_gate" and snap.gate_state == "pending":
            return
        if snap.gate_state == "pending":
            cleared += 1
            cond.gate_decide(task_id, action="approve",
                             reason="walk; independent re-run: pytest -> 1 failed",
                             override=True, actor=f"walk-{cleared}",
                             session_id=f"walk-{cleared}")
            continue
        cond.advance_task(task_id)


_GREEN_REASON = ("verify_green: pytest full suite passed (0 failed); oracle "
                 "EvidenceReceipt http_probe passed against the surface")


# ---------------------------------------------------------------------------
# 1. PINNED LOADER — worktree edit is IGNORED
# ---------------------------------------------------------------------------


def test_pinned_loader_ignores_worktree_edit(tmp_path):
    repo, baseline, wt = _policy_repo(tmp_path)
    # The candidate LOOSENS the rubric in its worktree.
    _write(wt, _RUBRIC_REL, _LOOSENED_RUBRIC)
    assert (wt / _RUBRIC_REL).read_text() == _LOOSENED_RUBRIC
    # The pinned loader reads the BASELINE content, not the worktree edit.
    pinned = cp.load_pinned_text(_RUBRIC_REL, baseline, repo)
    assert pinned == _PINNED_RUBRIC
    rubrics = cp.load_pinned_rubrics(ctx={"control_ref": baseline,
                                          "repo_root": str(repo)})
    assert rubrics["story_complete"]["required_sections"] == ["Summary"]


def test_policy_hash_stable_and_ref_resolves(tmp_path):
    repo, baseline, wt = _policy_repo(tmp_path)
    h1 = cp.policy_hash(baseline, repo)
    h2 = cp.policy_hash(baseline, repo)
    assert h1 and h1 == h2 and h1.startswith("sha256:")
    # Loosening the rubric AND committing it changes the pinned hash at the
    # new ref — policy content is what the hash tracks.
    _write(wt, _RUBRIC_REL, _LOOSENED_RUBRIC)
    _git(wt, "commit", "-qam", "loosen")
    edited_ref = _git(wt, "rev-parse", "HEAD")
    assert cp.policy_hash(edited_ref, repo) != h1


# ---------------------------------------------------------------------------
# 2. CANDIDATE-CONTROLS-JUDGE REFUSAL (the core tooth)
# ---------------------------------------------------------------------------


def test_gate_refuses_when_candidate_edits_policy(tmp_path):
    task_svc, cond = _services(tmp_path)
    repo, baseline, wt = _policy_repo(tmp_path)
    _write(wt, _RUBRIC_REL, _LOOSENED_RUBRIC)   # candidate edits its own judge
    t = task_svc.create(title="feature task that also loosens the rubric")
    _register_ws(t.id, wt, baseline, repo)
    _advance_to(cond, t.id, "story_gate")
    res = cond.gate_decide(t.id, "approve", reason="story looks good",
                           override=True, actor="s1", session_id="s1")
    assert res["ok"] is False and res["gate_state"] == "failed"
    assert "gate policy" in res["reason"].lower()
    assert "governance_rubrics.yaml" in res["reason"]
    assert task_svc.get(t.id).gate_state == "failed"


def test_gate_not_refused_when_only_nonpolicy_edited(tmp_path):
    task_svc, cond = _services(tmp_path)
    repo, baseline, wt = _policy_repo(tmp_path)
    _write(wt, _NONPOLICY_REL, "# non-policy source\nX = 2  # candidate edit\n")
    assert cp.candidate_policy_edits  # sanity
    t = task_svc.create(title="ordinary feature task")
    _register_ws(t.id, wt, baseline, repo)
    _advance_to(cond, t.id, "story_gate")
    res = cond.gate_decide(t.id, "approve", reason="story looks good",
                           override=True, actor="s1", session_id="s1")
    assert res["ok"] is True, res
    assert res["gate_state"] == "passed"   # cleared, then auto-advanced past
    assert task_svc.get(t.id).workflow_step != "story_gate"


def test_authorized_policy_change_env_allowed(tmp_path, monkeypatch):
    task_svc, cond = _services(tmp_path)
    repo, baseline, wt = _policy_repo(tmp_path)
    _write(wt, _RUBRIC_REL, _LOOSENED_RUBRIC)
    monkeypatch.setenv("PRISM_POLICY_CHANGE_APPROVED", "1")
    t = task_svc.create(title="authorized control-plane policy change")
    _register_ws(t.id, wt, baseline, repo)
    _advance_to(cond, t.id, "story_gate")
    res = cond.gate_decide(t.id, "approve", reason="approved policy change",
                           override=True, actor="s1", session_id="s1")
    assert res["ok"] is True, res


def test_authorized_policy_change_tag_allowed(tmp_path):
    task_svc, cond = _services(tmp_path)
    repo, baseline, wt = _policy_repo(tmp_path)
    _write(wt, _RUBRIC_REL, _LOOSENED_RUBRIC)
    t = task_svc.create(title="tagged policy change", tags=["policy-change"])
    _register_ws(t.id, wt, baseline, repo)
    _advance_to(cond, t.id, "story_gate")
    res = cond.gate_decide(t.id, "approve", reason="routed via control-plane",
                           override=True, actor="s1", session_id="s1")
    assert res["ok"] is True, res


# ---------------------------------------------------------------------------
# 3. PROVENANCE STAMPING + policy-hash staleness
# ---------------------------------------------------------------------------


def test_receipt_stamps_control_ref_and_policy_hash(tmp_path):
    task_svc, cond = _services(tmp_path)
    repo, baseline, wt = _policy_repo(tmp_path)
    url, srv = _serve()
    try:
        tid = str(uuid.uuid4())
        _register_ws(tid, wt, baseline, repo)
        task = types.SimpleNamespace(id=tid, oracle=url, likely_misfire=_MISFIRE)
        r = osp.run_oracle(osp.OracleSpec.from_task(task), task,
                           ctx={"project": "default", "workspace": str(wt)})
    finally:
        srv.shutdown()
    assert r.passed is True
    assert r.control_ref and r.policy_hash.startswith("sha256:")
    # Provenance survives the append-only round trip.
    back = osp.read_receipts("default", tid)[-1]
    assert back.policy_hash == r.policy_hash and back.control_ref == r.control_ref


def test_green_gate_refuses_receipt_under_different_policy_hash(tmp_path):
    task_svc, cond = _services(tmp_path)
    repo, baseline, wt = _policy_repo(tmp_path)   # clean worktree — no policy edit
    url, srv = _serve()
    try:
        t = task_svc.create(title="stale policy receipt", tags=["conductor"],
                            oracle=url, likely_misfire=_MISFIRE,
                            proof_type="test",
                            completion_proof=f"agent-browser {url}; docs/ok.png")
        _register_ws(t.id, wt, baseline, repo)
        _walk_to_green_gate(cond, t.id)
        live = task_svc.get(t.id)
        # Mint a PASSING receipt but stamped with a BOGUS pinned policy_hash —
        # tree_sha + spec_hash are fresh, only the policy pin differs.
        r = osp.run_oracle(osp.OracleSpec.from_task(live), live, ctx={
            "project": "default", "workspace": str(wt),
            "control_ref": "bogusref", "policy_hash": "sha256:BOGUSPOLICY"})
        assert r.passed is True and r.policy_hash == "sha256:BOGUSPOLICY"
    finally:
        srv.shutdown()
    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    assert res["ok"] is False and res["gate_state"] == "failed"
    assert "stale" in res["reason"].lower()


def test_green_gate_passes_and_stamps_pin_on_fresh_matching_receipt(tmp_path):
    task_svc, cond = _services(tmp_path)
    repo, baseline, wt = _policy_repo(tmp_path)
    url, srv = _serve()
    try:
        t = task_svc.create(title="fresh receipt under current pin",
                            tags=["conductor"], oracle=url,
                            likely_misfire=_MISFIRE, proof_type="test",
                            completion_proof=f"agent-browser {url}; docs/ok.png")
        _register_ws(t.id, wt, baseline, repo)
        _walk_to_green_gate(cond, t.id)
        live = task_svc.get(t.id)
        # No ctx policy override -> run_oracle resolves the REAL current pin.
        r = osp.run_oracle(osp.OracleSpec.from_task(live), live,
                           ctx={"project": "default", "workspace": str(wt)})
        assert r.passed is True and r.policy_hash.startswith("sha256:")
    finally:
        srv.shutdown()
    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    assert res["ok"] is True, res
    assert res.get("policy_hash", "").startswith("sha256:")
    assert res.get("control_ref")


# ---------------------------------------------------------------------------
# 4. Regression guards on the already-present guarantees
# ---------------------------------------------------------------------------


def test_distinct_actor_still_refused(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="steward independent")
    _advance_to(cond, t.id, "story_gate")
    task_svc.link_session(t.id, "S1")
    res = cond.gate_decide(t.id, "approve", reason="i wrote it, trust me",
                           override=True, actor="S1", session_id="S1")
    assert res["ok"] is False
    reason = (res.get("reason") or "").lower()
    assert "actor" in reason and (
        "independent" in reason or "distinct" in reason or "same-actor" in reason)


def test_sessionless_flow_report_rejected(tmp_path, monkeypatch):
    """Regression on #1: a report with no session identity is rejected before
    any advance (distinct-actor enforcement needs a named actor)."""
    import prism_service.api.conductor_flow as cf
    monkeypatch.setattr(cf, "_svc", lambda project: object())
    body = cf.Ident(task_id="x", session_id=None, expected_step="green_gate",
                    outcome="ok")
    res = cf.flow_report(body, project="default")
    assert res["ok"] is False and "session_id is required" in res["error"]


def test_receipts_still_append_only(tmp_path):
    tid = str(uuid.uuid4())
    task = types.SimpleNamespace(id=tid, oracle="see it render", likely_misfire="")
    spec = osp.OracleSpec.from_task(task)
    osp.run_oracle(spec, task, ctx={"project": "default"})
    osp.run_oracle(spec, task, ctx={"project": "default"})
    assert len(osp.read_receipts("default", tid)) == 2


def test_unrunnable_oracle_is_manual_not_policy_pass(tmp_path):
    task = types.SimpleNamespace(id=str(uuid.uuid4()),
                                 oracle="you can SEE the dashboard render",
                                 likely_misfire="")
    spec = osp.OracleSpec.from_task(task)   # no URL -> browser adapter
    assert spec.adapter == osp.ADAPTER_BROWSER
    r = osp.run_oracle(spec, task, ctx={"project": "default"})
    assert r.status == osp.ST_MANUAL and r.passed is False
