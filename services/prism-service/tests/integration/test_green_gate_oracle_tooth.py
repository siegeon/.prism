"""RED scaffold — oracle-bound green_gate guardrail (task 3eb67fb3).

ROOT FAILURE this pins (from task b521af05): a drive marked `verify_green`
on "unit tests pass + endpoints work" and presented `green_gate`, but NOBODY
exercised the task's ORACLE ("a /brain that loads without crashing"). The
page still crashed the owner's browser. Tests-green != oracle-met = a false
green that reached the human.

TODAY the oracle/misfire checks at green_gate are ADVISORY only: on a pass,
`green_gate_proof_note` (conductor_service.py:46) and `green_gate_misfire_note`
(:63) merely `+=` a "⚠" note to the passing reason (conductor_service.py:
2159-2201). They NEVER refuse. And the existing artifact tooth
(`green_gate_artifact_reason`, :379) is satisfied by a test-runner+pass signal
found in EITHER `completion_proof` OR the decision `reason`
(`_artifact_text`, :310) — so a green-looking approve `reason` clears it even
when the task's STORED `completion_proof` is empty or cites neither the oracle
nor the misfire. Net: a non-compliant proof PASSES today.

The guardrail (NOT built yet) must promote those advisory notes to a real,
blocking tooth in `ConductorService.gate_decide` — REFUSE (gate_state
"failed") on a green_gate approve when the task's `completion_proof` is empty,
does not cite the observable named in `task.oracle`, or does not address the
pre-declared `task.likely_misfire`; PASS when it does both.

Test A/B below are the LOAD-BEARING RED assertions: they drive the REAL
`ConductorService.gate_decide(approve)` on a real pending green_gate and assert
a REFUSAL. TODAY the gate PASSES the non-compliant proof, so those assertions
FAIL == RED. Test C guards against over-strictness (a compliant proof must
still pass) and is expected GREEN now and after the fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# The fixture task's honest oracle + declared misfire, reused across cases.
_ORACLE = ("the /brain page loads at http://localhost:8888/brain without "
           "crashing")
_MISFIRE = ("the /brain page crashes the browser tab on load with a blank "
            "white screen")


def _services(tmp_path):
    """A REAL ConductorService wired to a REAL TaskService, NO verifier
    (the plan's endorsed refuse-path setup — the tooth reads the live task's
    oracle/proof/misfire, not a verifier). The final green_gate approve takes
    the legacy trust-caller path (conductor_service.py:2055) so the ONLY thing
    that can flip the verdict is the artifact tooth + the (missing) oracle
    tooth — never a mocked verifier verdict."""
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"),
        enable_engine=False,
        task_svc=task_svc,
        verifier_svc=None,
    )
    return task_svc, cond


def _green_gate_id() -> str:
    from prism_service.models.workflow import WORKFLOW_STEPS

    return next(s["id"] for s in WORKFLOW_STEPS
                if s["type"] == "gate" and s["id"] == "green_gate")


def _walk_to_green_gate(cond, task_id: str) -> None:
    """Advance to a pending green_gate, clearing each earlier pending gate via
    a DISTINCT-actor override so the test isolates the green_gate decision
    (story_gate/plan_gate/red_gate precede it; a reused actor self-overrides ->
    refused)."""
    from prism_service.models.workflow import WORKFLOW_STEPS

    target_idx = next(i for i, s in enumerate(WORKFLOW_STEPS)
                      if s["id"] == _green_gate_id())
    guard = (target_idx + 1) * 3
    cleared = 0
    while guard > 0:
        guard -= 1
        snap = cond._task_svc.get(task_id)
        if snap.workflow_step == _green_gate_id() and snap.gate_state == "pending":
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


def _make_green_gate_task(task_svc, cond, **kw):
    """Create a fixture task carrying the oracle + misfire and drive it to a
    pending green_gate. Proof_type 'test' keeps it OFF the ui-artifact tooth
    (STRAND C, which only fires for `ui`-tagged tasks) so the case exercises
    the generic green_gate close, not the ui path."""
    t = task_svc.create(
        title=kw.pop("title", "oracle-bound green_gate fixture"),
        tags=kw.pop("tags", ["conductor"]),
        oracle=kw.pop("oracle", _ORACLE),
        likely_misfire=kw.pop("likely_misfire", _MISFIRE),
        proof_type=kw.pop("proof_type", "test"),
        completion_proof=kw.pop("completion_proof", ""),
        **kw,
    )
    _walk_to_green_gate(cond, t.id)
    return t


# ── Test A (RED now): EMPTY completion_proof must be REFUSED ──────────────

def test_green_gate_refuses_empty_completion_proof(tmp_path):
    """AC-1/AC-4: a green_gate approve on a task whose stored
    `completion_proof` is EMPTY must be REFUSED (gate_state 'failed'), closing
    the b521af05 false-green.

    RED TODAY: the approve `reason` carries a runner+pass signal, so the
    existing artifact tooth is satisfied via `_artifact_text` even though the
    task's OWN completion_proof is empty; the oracle/proof advisory note only
    appends "⚠", never blocks. So gate_decide PASSES today and this assertion
    FAILS == RED. After the tooth lands it fail-closes on the empty proof."""
    task_svc, cond = _services(tmp_path)
    t = _make_green_gate_task(task_svc, cond, completion_proof="")

    result = cond.gate_decide(
        t.id, action="approve",
        reason="pytest tests/unit/test_brain.py passed — all green",
    )

    assert result["ok"] is False, (
        "green_gate PASSED a close with an EMPTY completion_proof — the "
        "oracle was never evidenced (false green reaches the human, exactly "
        f"the b521af05 failure). result={result!r}"
    )
    # Owner 2026-07-19: a refused approve stays PENDING (retryable), not failed.
    assert result["gate_state"] == "pending"
    refreshed = task_svc.get(t.id)
    assert refreshed.gate_state == "pending"
    assert refreshed.workflow_step == _green_gate_id()
    reason = (result.get("reason") or "").lower()
    assert "oracle" in reason or "proof" in reason, (
        "refusal reason must name the missing proof/oracle so the operator "
        f"knows WHY (got {result.get('reason')!r})"
    )


# ── Test B (RED now): proof that ignores oracle + misfire is REFUSED ──────

def test_green_gate_refuses_proof_that_ignores_oracle_and_misfire(tmp_path):
    """AC-1/AC-5: a NON-empty completion_proof that cites NEITHER the oracle's
    observable (the http://localhost:8888/brain URL) NOR the declared misfire
    must be REFUSED.

    RED TODAY: "all unit tests pass" is a self-attested green with no oracle
    citation and no misfire word; the approve `reason` satisfies the artifact
    tooth, and the misfire/proof checks are advisory-only, so gate_decide
    PASSES today and this REFUSAL assertion FAILS == RED. This is the core
    'tests-green != oracle-met' tooth."""
    task_svc, cond = _services(tmp_path)
    t = _make_green_gate_task(
        task_svc, cond, completion_proof="all unit tests pass")

    result = cond.gate_decide(
        t.id, action="approve",
        reason="pytest suite green, 0 failed",
    )

    assert result["ok"] is False, (
        "green_gate PASSED a completion_proof that cites neither the oracle "
        f"observable ({_ORACLE!r}) nor the misfire — tests-green is not "
        f"oracle-met. result={result!r}"
    )
    # Owner 2026-07-19: a refused approve stays PENDING (retryable), not failed.
    assert result["gate_state"] == "pending"
    assert task_svc.get(t.id).gate_state == "pending"


# ── Test C (GREEN after fix): a FRESH PASSING RECEIPT still PASSES ─────────
#
# NOTE (inverted-flow #2): the deciding authority is no longer the token
# scorer — it is a fresh passing EvidenceReceipt from a REAL run. A compliant
# prose proof ALONE no longer passes (that was the gameable defect). This test
# now stands up a live surface, runs the oracle for real to mint a passing
# receipt, and asserts the gate is then claimable — guarding against the
# over-strict failure mode (a genuinely-evidenced oracle must still pass).

def test_green_gate_passes_on_fresh_passing_receipt(tmp_path):
    """AC-2: an oracle-bearing task whose oracle was actually RUN (a fresh
    passing EvidenceReceipt against a live surface) must PASS — the tooth has
    teeth WITHOUT rejecting a genuinely-evidenced close."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from prism_service.services import oracle_spec as osp

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>brain page renders fine</html>")

        def log_message(self, *a):
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/brain"

    task_svc, cond = _services(tmp_path)
    proof = (f"agent-browser loaded {url} — renders without crashing; NOT a "
             "blank white screen (screenshot docs/brain_loads.png)")
    try:
        t = _make_green_gate_task(task_svc, cond, oracle=url,
                                  completion_proof=proof)
        live = task_svc.get(t.id)
        receipt = osp.run_oracle(osp.OracleSpec.from_task(live), live,
                                 ctx={"project": "default"})
        assert receipt.passed is True, receipt
    finally:
        srv.shutdown()

    result = cond.gate_decide(
        t.id, action="approve",
        reason=("verified: agent-browser walkthrough of the /brain surface, "
                "page loads, no crash; pytest suite green"),
    )

    assert result["ok"] is True, (
        "a task whose oracle was RUN (fresh passing receipt) was REFUSED — "
        f"the tooth is over-strict. result={result!r}"
    )
    assert result["gate_state"] == "passed"
    assert task_svc.get(t.id).gate_state == "passed"
