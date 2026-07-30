"""Machine adjudicator seat vs. the visual-observable floor (task ee5b226d).

`ConductorService.adjudicate_green_gate` is the ONE chokepoint every real
conductor_work green_gate report passes through (conductor.py's readiness
endpoint and conductor_service's other gate teeth all consult the same
`is_human_judgment(OracleSpec.from_task(task))` primitive this ticket
fixes). These tests drive that REAL method end to end — no direct call to
`_names_visual_observable` or `OracleSpec.from_task` — so a passing suite
here is evidence the fix is actually WIRED into the seat that decides
gates, not just correct in isolation (test_oracle_spec.py pins that half).

  AC-1  a visual-observable oracle citing a URL (the 89e90d1a shape) ->
        the adjudicator DECLINES (returns None); the gate stays pending
        for a human, exactly as a no-URL manual_evidence_required oracle
        already does.
  AC-2  the same shape but asserting a row's ABSENCE (the fresh recurrence
        shape) -> also declined.
  AC-3  likely_misfire guard: a genuine reachability oracle citing a real
        URL still gets MINTED and APPROVED by the adjudicator — the fix
        must not strand machine-checkable tickets on a human click.
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _gated_task(tmp_path, oracle, proof_type=""):
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="adjudicate me", oracle=oracle,
                        proof_type=proof_type)
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending")
    return task_svc, task_svc.get(t.id)


def _conductor(tmp_path, task_svc):
    from prism_service.services.conductor_service import ConductorService
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = "testproj"
    return cond


# ---------------------------------------------------------------------------
# AC-1 / AC-2 — visual-observable oracles stay pending for a human
# ---------------------------------------------------------------------------

_VISUAL_ORACLE = (
    "Open http://127.0.0.1:8888/tasks?project=prism and confirm the LiveBar "
    "shows skeleton/loading placeholders and holds a neutral loading tint. "
    "A screenshot proves NO 0-valued KPI tile is ever shown."
)

_ABSENT_ROW_ORACLE = (
    "Open http://127.0.0.1:8888/tasks?project=prism and confirm no "
    "imported PR title appears as a task in the list."
)


def test_visual_url_oracle_gate_stays_pending(tmp_path):
    task_svc, task = _gated_task(tmp_path, oracle=_VISUAL_ORACLE)
    cond = _conductor(tmp_path, task_svc)
    res = cond.adjudicate_green_gate(task.id)
    assert res is None, (
        "the adjudicator must not auto-approve a visual oracle even "
        "though a URL is present in the oracle text")
    after = task_svc.get(task.id)
    assert after.gate_state == "pending"
    assert after.workflow_step == "green_gate"


def test_absent_row_oracle_gate_stays_pending(tmp_path):
    task_svc, task = _gated_task(tmp_path, oracle=_ABSENT_ROW_ORACLE)
    cond = _conductor(tmp_path, task_svc)
    res = cond.adjudicate_green_gate(task.id)
    assert res is None
    assert task_svc.get(task.id).gate_state == "pending"


def test_visual_oracle_gate_stays_pending_even_with_a_passing_receipt_forced(
        tmp_path, monkeypatch):
    """Mirrors the recorded eaafdf75 guard: even if some other path had
    minted a passing receipt, the human-judgment classification is checked
    BEFORE the receipt tooth, so a forced pass still cannot auto-approve a
    visual gate."""
    task_svc, task = _gated_task(tmp_path, oracle=_VISUAL_ORACLE)
    cond = _conductor(tmp_path, task_svc)

    class _R:
        adapter = "browser"
        job_id = "forced-pass"
        tree_sha = "deadbeef"
        spec_hash = "sha256:fake"
        passed = True
        status = "passed"
        policy_hash = ""
        reason = "forced pass (test double)"
    monkeypatch.setattr(cond, "_oracle_receipt_refusal",
                        lambda *a, **k: ("", _R()))
    res = cond.adjudicate_green_gate(task.id)
    assert res is None
    assert task_svc.get(task.id).gate_state == "pending"


# ---------------------------------------------------------------------------
# AC-3 — likely_misfire guard: a genuine reachability oracle still gets
# minted + approved (no blanket over-correction to manual review)
# ---------------------------------------------------------------------------

def _serve(status=200, body=b"<html>ok, version 7.8.0</html>"):
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/", srv


def test_reachability_oracle_gate_is_minted_and_approved(tmp_path,
                                                          monkeypatch):
    from prism_service.services import oracle_spec as osp

    url, srv = _serve()
    try:
        oracle = f"GET {url} returns 200 with the current build info"
        task_svc, task = _gated_task(tmp_path, oracle=oracle)
        # Proof-carrying-artifact tooth (a SEPARATE green_gate tooth, not
        # part of this ticket's fix): the approve reason the adjudicator
        # writes needs a runner+pass signal on file. A driver reporting a
        # machine-checkable http_probe oracle would cite its own re-run
        # here; this is the honest equivalent, not a bypass.
        task_svc.update(task.id,
                        completion_proof="http_probe verified: pytest -q "
                                        "-> 1 passed, reachable 200 OK")
        task = task_svc.get(task.id)
        spec = osp.OracleSpec.from_task(task)
        assert spec.adapter == osp.ADAPTER_HTTP, (
            "sanity: this oracle must derive http_probe, not browser")

        cond = _conductor(tmp_path, task_svc)

        def _mint(task_id, session_id=None, model=None, release=False):
            t = task_svc.get(task_id)
            s = osp.OracleSpec.from_task(t)
            osp.run_oracle(s, t, ctx={"project": "testproj"})
            return {"ok": True}

        monkeypatch.setattr(cond, "mint_green_evidence", _mint)
        res = cond.adjudicate_green_gate(task.id)
        assert res is not None and res.get("ok") is True, (
            f"a genuine reachability oracle must still be minted and "
            f"approved by the machine seat: {res}")
        after = task_svc.get(task.id)
        assert after.gate_state == "passed"
        assert after.status == "done"
    finally:
        srv.shutdown()
