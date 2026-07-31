"""Bare conductor_work loop drives a real task to done on machine-verified
green (task ae63e375, split from 7b219546's surface half).

TWO ROOT CAUSES under test:
  1. pending->done desync: a task reaching workflow_step=green_gate with
     gate_state=passed used to sit at status=pending forever (never left
     the board). ConductorService.gate_decide now flips status='done'
     (stamping completed_at) in the SAME update that persists the passed
     terminal gate — this test's final assertions pin that.
  2. no-machine-oracle terminal honesty: the terminal green_gate must clear
     on a REAL, machine-run oracle receipt (oracle_spec.run_oracle, wired
     automatically by conductor_flow.flow_report's verify_green_state ->
     ConductorService.mint_green_evidence -> VerifierService.run_green_lanes)
     — NOT a manual override. This task's oracle names a real local HTTP
     server, so OracleSpec.from_task derives a REAL http_probe adapter, and
     the terminal green_gate report below passes override=False.

ANTI-MISFIRE (the task's own pre-declared risk): this must NOT reach done
the way test_conductor_work_terminal.py's _drive_to_done does — overriding
EVERY gate including the terminal one. Earlier (non-terminal) gates below
use the audited distinct-actor override, a legitimate production recovery
path this task does not test; the terminal green_gate is cleared for REAL.
"""
from __future__ import annotations

import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _serve(status: int = 200,
          body: str = "<html>ok honest oracle surface</html>"):
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


def _terminal(task) -> bool:
    """The EXACT `done` predicate conductor_work's MCP handler computes."""
    from prism_service.models.workflow import WORKFLOW_STEPS
    if str(getattr(task, "status", "") or "") in ("done", "cancelled"):
        return True
    last = WORKFLOW_STEPS[-1]["id"]
    return (getattr(task, "workflow_step", "") == last
            and getattr(task, "gate_state", "") == "passed")


# Non-terminal gates: cleared via the audited distinct-actor override — a
# legitimate production recovery path, and NOT what this task tests. The
# terminal green_gate (handled separately below) never uses override.
_NON_TERMINAL_GATE_REPORTS = {
    "story_gate": "story reviewed and accepted by an independent steward",
    "plan_gate": "plan reviewed and accepted by an independent steward",
    "red_gate": ("red trace observed: baseline 0 -> candidate 1 failing "
                "(metric receipt)"),
}

_GREEN_GATE_REASON = ("oracle receipt: 1/1 checks passed (100% -> 0% "
                      "failure rate); green_full verified via the 3-lane "
                      "signal")
_GREEN_PROOF = ("oracle receipt: 1/1 checks passed (100% -> 0% failure "
                "rate) against the health endpoint")


def test_bare_loop_drives_real_task_to_done_on_machine_verified_green():
    from prism_service.project_context import get_project
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_workspace

    url, srv = _serve()
    project = "cwl-" + uuid.uuid4().hex[:8]
    task_svc = get_project(project).task_svc
    task = task_svc.create(
        title="health endpoint reports ok",
        tags=["conductor"],
        oracle=f"GET {url} returns 200 with a healthy, non-empty body",
        proof_type="metric",
    )
    task_id = task.id
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this honest-green loop walk (unrelated to premise content)
    # can leave review_previous_notes.
    task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising the bare loop driving a "
        "task to machine-verified done, not a real premise claim - "
        "UNVERIFIED\n"))
    seat = 0

    try:
        start = cf.flow_start(
            cf.Ident(task_id=task_id, session_id="builder"), project=project)
        assert start["ok"] is True, start
        job = start["job"]
        guard = 40
        csvc = get_project(project).conductor_svc
        while not _terminal(task_svc.get(task_id)):
            assert guard > 0, "loop did not terminate"
            guard -= 1
            job = cf.flow_next(task_id, project=project)["job"]
            assert job is not None, "no job on deck but task not terminal"
            step, kind = job["step"], job["kind"]
            if kind != "gate":
                # Agent step: the builder reports success; the server advances.
                rep = cf.flow_report(cf.Ident(
                    task_id=task_id, session_id="builder",
                    expected_step=step, outcome="done"), project=project)
                assert rep.get("ok") is not False, rep
                continue
            # Gate: a DISTINCT actor decides (never the builder).
            seat += 1
            actor = f"reviewer-{seat}"
            if step != "green_gate":
                # Non-terminal gate: audited distinct-actor override — a
                # legitimate recovery path, and explicitly NOT what this
                # task tests. Only the terminal gate must be machine-real.
                task_svc.update(
                    task_id,
                    completion_proof=_NON_TERMINAL_GATE_REPORTS.get(step, "ok"))
                rep = cf.flow_report(cf.Ident(
                    task_id=task_id, session_id=actor, expected_step=step,
                    outcome=_NON_TERMINAL_GATE_REPORTS.get(step, "approved"),
                    override=True), project=project)
                assert rep.get("ok") is not False, rep
                continue
            # TERMINAL green_gate — the honest crux. Mint a REAL oracle
            # receipt from the task's worktree (http_probe GETs our live
            # surface), then clear the gate with override=False so it passes
            # on machine merit, not a manual bypass.
            mint = csvc.mint_green_evidence(task_id)
            assert mint["ok"] is True, (
                f"green lanes must mint a PASSING oracle receipt: {mint}")
            task_svc.update(task_id, completion_proof=_GREEN_PROOF)
            rep = cf.flow_report(cf.Ident(
                task_id=task_id, session_id=actor, expected_step=step,
                outcome=_GREEN_GATE_REASON, override=False), project=project)
            assert rep.get("ok") is not False, (
                f"terminal green_gate must pass with override=False: {rep}")
    finally:
        srv.shutdown()

    final = task_svc.get(task_id)
    # Root cause #1: the terminal pass flips status->done (leaves the board).
    assert final.status == "done", (
        f"pending->done desync NOT fixed: status={final.status!r}")
    assert final.completed_at, "completed_at must be stamped at terminal done"
    # Terminal gate really passed...
    assert final.workflow_step == "green_gate" and final.gate_state == "passed", (
        f"step={final.workflow_step} gate={final.gate_state}")
    # Root cause #2: ...and NOT via a manual override (the pre-declared misfire).
    assert "override" not in (final.gate_reason or "").lower(), (
        f"terminal gate must pass on MACHINE merit, not override: "
        f"{final.gate_reason!r}")
