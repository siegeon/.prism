"""An owner's gate click leaves an honest record (task 98d38111).

Sibling tasks under epic 37c9207b already fixed two of three record defects
observed 2026-08-09 on epic 02672417's plan_gate Approve click:

  (1) a plain plan_gate Approve now also records the design-packet ledger's
      own owner_explicit approval (task 791602a9) - REGRESSION pin here.
  (3) GET /api/conductor/gate/readiness at plan_gate serves a design-packet-
      shaped receipt, never an epic-rollup/pytest-EvidenceReceipt shape
      (task 457b38db et al) - REGRESSION pin here.

Defect (2) is STILL LIVE and is the crux of this suite: TaskDetailPage.tsx's
gateDecide POSTs to /api/conductor/gate with NO `actor` and NO `session_id`
at all, and calls approveDesignPacket with the boilerplate one-click reason
text as the `approver` - never the browser's real signed-in identity. The
server (conductor_service.py _decided_by, task 1bc13307) already persists
whatever actor string it is given verbatim; the resolver (actor_service.py)
already joins a real user id/email to ActorKind.HUMAN. Nothing server-side
needs to change - the browser's real identity simply never reaches either
wire call, so every human gate click resolves to unknown:conductor.

Group A pins the defect at the actual affordance a human owner clicks - the
Approve button's gateDecide handler - by reading the ACTUAL TSX source (this
repo's SPA has no JS test runner; see
tests/unit/test_conductor_page_animated_cleanup_ui.py). Group B is a real
drive over HTTP (FastAPI TestClient, real ConductorService/TaskService/
WorkspaceService/ActorService) proving the backend already does the right
thing once given a real identity, and pinning the two already-fixed
regressions plus the _ALLOWED_METHODS/machine-adjudication guard rails.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_TASK_DETAIL_PAGE = _WEB_SRC / "pages" / "TaskDetailPage.tsx"


# ---------------------------------------------------------------------------
# Group A - source-reading pins on the real UI entry point (Approve button ->
# gateDecide -> the two wire calls). No JS runner in this repo (documented
# convention: UI ACs are pinned against the ACTUAL TSX source).
# ---------------------------------------------------------------------------


def _task_detail_source() -> str:
    return _TASK_DETAIL_PAGE.read_text(encoding="utf-8")


def _gate_decide_body(src: str) -> str:
    """Isolate the gateDecide handler body (the Approve/Reject click
    handler) so assertions never accidentally match an unrelated fetch
    elsewhere in this large file."""
    start = src.index("const gateDecide = async")
    end_marker = "    } finally {\n      setBusy(false);\n    }\n  };"
    end = src.index(end_marker, start) + len(end_marker)
    return src[start:end]


def test_gate_post_body_carries_a_resolved_actor_identity():
    """DEFECT (2), crux: gateDecide's POST to /api/conductor/gate must carry
    an `actor` field sourced from the browser's real signed-in identity - the
    server falls to the literal 'conductor' (conductor_service.py
    _decided_by) whenever both actor and session_id are absent, and that
    resolves ActorKind.UNKNOWN (actor_service.py:48-50). Today the body is
    exactly {task_id, action, reason, override} - no actor, no session_id."""
    body = _gate_decide_body(_task_detail_source())
    call_at = body.index("fetch(`/api/conductor/gate")
    obj_at = body.index("body: JSON.stringify({", call_at)
    obj_end = body.index("}),", obj_at)
    payload = body[obj_at:obj_end]
    assert re.search(r"\bactor\s*:", payload), (
        "gateDecide's POST body to /api/conductor/gate carries no `actor` "
        f"field - every human click will resolve to unknown:conductor. "
        f"payload literal:\n{payload}"
    )


def test_task_detail_page_fetches_the_real_signed_in_identity():
    """A real identity must exist somewhere in this component before
    gateDecide can forward it - PageHeader.tsx's IdentityChip already shows
    the pattern (api.get('/api/auth/me')). TaskDetailPage.tsx has no such
    call today."""
    src = _task_detail_source()
    assert "/api/auth/me" in src, (
        "TaskDetailPage.tsx never fetches the signed-in owner's identity - "
        "gateDecide has nothing real to put in `actor`/`approver`"
    )


def test_design_packet_approve_receives_a_real_identity_not_boilerplate_text():
    """The design-packet-approve call's `approver` argument must be the real
    identity, never the one-click boilerplate reason text ('approved by
    owner (one-click from the task page)') - that string is not an approver
    identity, it is the AUDIT REASON, and today it is passed as both."""
    body = _gate_decide_body(_task_detail_source())
    m = re.search(r"approveDesignPacket\(([^)]*)\)", body)
    assert m, "gateDecide no longer calls approveDesignPacket at all"
    args = [a.strip() for a in m.group(1).split(",")]
    assert len(args) >= 2, args
    assert args[1] != "decisionReason", (
        "approveDesignPacket's approver argument is still the boilerplate "
        f"decisionReason text, not a real identity: {m.group(0)!r}"
    )


def test_prefill_reason_gates_oracle_receipt_language_on_live_readiness():
    """REGRESSION (already true): the pre-filled Approve reason must never
    cite an oracle/EvidenceReceipt adapter string while plan_gate is still
    awaiting design-packet approval - it is gated on gateReadiness's OWN
    receipt_ok (which the design-packet readiness branch, api/conductor.py
    :336-353, only ever reports True once approved), never on
    isAwaitingDesignApproval alone."""
    src = _task_detail_source()
    start = src.index("setGateReason(isAwaitingReview")
    end = src.index(");", start) + 2
    snippet = src[start:end]
    assert "receipt_ok" in snippet, snippet


# ---------------------------------------------------------------------------
# Group B - a real drive over HTTP: real ConductorService/TaskService/
# WorkspaceService/ActorService wired through FastAPI TestClient. Proves the
# backend already handles a real identity correctly end to end, and pins the
# two already-fixed regressions plus the negative guard rails.
# ---------------------------------------------------------------------------


def _wired(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import conductor as conductor_api
    from prism_service.api import tasks as tasks_api
    from prism_service.api.auth import authorize_project_request, current_principal
    from prism_service.models.workspace import Principal
    from prism_service.services import actor_service as actor_service_module
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService
    from prism_service.services.workspace_service import WorkspaceService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    # ProjectContext always wires a real verifier (conductor_service.py:3546-
    # 3555's docstring); a bare no-verifier ConductorService takes a LEGACY
    # branch that hardcodes actor="conductor" unconditionally (never
    # _decided_by) - that is a test-construction artifact, not the real
    # gate_decide path a human's Approve click drives in production. Stub
    # _verify_gate green so gate_decide takes the SAME verifier-driven path
    # (:3556-3632, which DOES use _decided_by) that production takes.
    cond._verifier_svc = object()  # attach ANY verifier so the branch below
    # (elif self._verifier_svc is None:) is skipped - that legacy no-verifier
    # branch hardcodes actor="conductor" unconditionally, a test-construction
    # artifact production never hits (ProjectContext always wires one).
    monkeypatch.setattr(
        cond, "_verify_gate",
        lambda t, step_id, proof_type=None: {
            "verified": True, "reason": "stub rubric green",
            "verifier": None, "validation": "plan_coverage"})
    ws = WorkspaceService(tmp_path / "workspace.db")
    owner = ws.create_user("owner@example.com", display_name="Task Owner")
    real_actor_svc = actor_service_module.ActorService(workspaces=ws)

    monkeypatch.setattr(conductor_api, "_svc", lambda project: cond)
    monkeypatch.setattr(tasks_api, "_svc", lambda project: task_svc)
    monkeypatch.setattr(tasks_api, "get_actor_service", lambda: real_actor_svc)
    monkeypatch.setattr(actor_service_module, "get_actor_service",
                        lambda: real_actor_svc)

    principal = Principal(user_id=owner.id, email=owner.email,
                          display_name=owner.display_name, mode="local",
                          role="owner")
    app = FastAPI()
    app.include_router(conductor_api.router, prefix="/api/conductor")
    app.include_router(tasks_api.router, prefix="/api/tasks")
    app.dependency_overrides[authorize_project_request] = lambda: principal
    app.dependency_overrides[current_principal] = lambda: principal

    return TestClient(app), task_svc, cond, owner


def _plan_gate_task(task_svc, title="owner gate click record"):
    t = task_svc.create(title=title, oracle="oracle text", proof_type="demo",
                        tags=["ui"])
    task_svc.update(t.id, workflow_step="plan_gate", gate_state="pending",
                    plan_doc="## Plan\nAC-1 covered",
                    plan_diagram="stateDiagram-v2\n[*]-->A")
    return task_svc.get(t.id)


def test_owner_click_records_design_approval_and_a_human_history_row(
        tmp_path, monkeypatch):
    """The full real drive: root task at plan_gate -> the two wire calls the
    FIXED gateDecide makes (design-packet approve, then the gate approve),
    each carrying the real owner identity -> read back both the design-
    packet ledger AND the gate_decide history row."""
    client, task_svc, cond, owner = _wired(tmp_path, monkeypatch)
    task = _plan_gate_task(task_svc)
    project = "default"

    # REGRESSION (task 457b38db et al): readiness at plan_gate speaks only
    # design-packet language, never an epic-rollup/pytest-EvidenceReceipt shape.
    r0 = client.get("/api/conductor/gate/readiness",
                    params={"task_id": task.id, "project": project})
    assert r0.status_code == 200, r0.text
    receipt0 = r0.json().get("receipt") or {}
    assert receipt0.get("adapter") == "design-packet", receipt0
    assert "tier0" not in receipt0 and "pytest_ids" not in receipt0

    r1 = client.post("/api/conductor/design-packet/approve",
                     params={"project": project},
                     json={"task_id": task.id, "approver": owner.email})
    assert r1.status_code == 200, r1.text
    assert r1.json()["ok"] is True, r1.json()

    r2 = client.post("/api/conductor/gate", params={"project": project},
                     json={"task_id": task.id, "action": "approve",
                          "reason": "approved by owner (one-click from the task page)",
                          "override": False, "actor": owner.email})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("ok") is True, r2.json()

    # REGRESSION (task 791602a9): the design-packet ledger recorded an
    # owner_explicit approval.
    from prism_service.services import design_packet as dp
    latest = dp.latest_approval(project, task.id)
    assert latest is not None, "no design-packet approval was recorded"
    assert latest.method == "owner_explicit"
    assert latest.approver == owner.email

    packet = client.get("/api/conductor/design-packet",
                        params={"task_id": task.id, "project": project}).json()
    assert packet["approval"]["approved"] is True, packet["approval"]

    # THE CRUX: the gate_decide history row resolves to a real HUMAN, not
    # unknown:conductor.
    detail = client.get(f"/api/tasks/{task.id}",
                        params={"project": project}).json()
    gate_rows = [h for h in detail["history"] if h["action"] == "gate_decide"]
    assert gate_rows, "no gate_decide history row recorded"
    last = gate_rows[-1]
    assert last["actor"] == owner.email, last
    identity = last["actor_identity"]
    assert identity["kind"] == "human", (
        f"expected a resolvable HUMAN actor, got {identity!r} - this is "
        "exactly the unknown:conductor defect this task exists to close")
    assert not str(identity["id"]).startswith("unknown:"), identity


def test_design_packet_approve_refuses_an_unresolvable_approver(
        tmp_path, monkeypatch):
    """AC-5 (plan_doc, section 2.C): the design-packet-approve ROUTE must
    resolve `approver` through ActorService before recording it, refusing
    (4xx) an approver that is not a real human identity - e.g. the old
    boilerplate reason text - rather than writing an owner_explicit receipt
    for nobody. Today the route forwards ANY non-empty string straight to
    record_approval (api/conductor.py:220-239) with no resolution at all."""
    client, task_svc, cond, owner = _wired(tmp_path, monkeypatch)
    task = _plan_gate_task(task_svc)
    project = "default"
    boilerplate = "approved by owner (one-click from the task page)"

    r = client.post("/api/conductor/design-packet/approve",
                    params={"project": project},
                    json={"task_id": task.id, "approver": boilerplate})

    assert r.status_code >= 400, (
        f"an unresolvable approver identity was accepted: {r.status_code} "
        f"{r.text}")

    from prism_service.services import design_packet as dp
    assert dp.latest_approval(project, task.id) is None, (
        "an approval receipt was written for an unresolvable approver")
    status = dp.approval_status(project, task.id, task_svc.get(task.id))
    assert status["approved"] is False, status


def test_gate_approve_without_actor_or_session_id_stays_unresolved(
        tmp_path, monkeypatch):
    """AC-6, negative: the server must never invent an owner. A gate approve
    carrying neither `actor` nor `session_id` records NO human identity."""
    client, task_svc, cond, owner = _wired(tmp_path, monkeypatch)
    task = _plan_gate_task(task_svc)
    project = "default"

    r = client.post("/api/conductor/gate", params={"project": project},
                    json={"task_id": task.id, "action": "approve",
                         "reason": "no identity supplied", "override": False})
    assert r.status_code == 200, r.text

    detail = client.get(f"/api/tasks/{task.id}",
                        params={"project": project}).json()
    gate_rows = [h for h in detail["history"] if h["action"] == "gate_decide"]
    assert gate_rows, "no gate_decide history row recorded"
    identity = gate_rows[-1]["actor_identity"]
    assert identity["kind"] != "human", identity
    assert not str(identity["id"]).startswith("user:"), identity


def test_actor_identity_kind_alone_distinguishes_human_from_machine_seat(
        tmp_path, monkeypatch):
    """A reader must be able to tell a human gate decision from a machine
    seat's by reading actor_identity.kind ALONE - no raw-string sniffing."""
    client, task_svc, cond, owner = _wired(tmp_path, monkeypatch)
    task = _plan_gate_task(task_svc)
    task_svc.record_history(task.id, actor="conductor-adjudicator",
                            action="gate_decide", details="machine approved")
    task_svc.record_history(task.id, actor=owner.email,
                            action="gate_decide", details="human approved")

    detail = client.get(f"/api/tasks/{task.id}",
                        params={"project": "default"}).json()
    rows = {h["actor"]: h["actor_identity"]["kind"] for h in detail["history"]
           if h["action"] == "gate_decide"}
    assert rows["conductor-adjudicator"] == "machine", rows
    assert rows[owner.email] == "human", rows
    assert rows["conductor-adjudicator"] != rows[owner.email]


def test_design_packet_allowed_methods_stays_owner_explicit_only():
    """NEGATIVE guard: the fix must never widen the set of approval methods
    to make the actor-identity gap disappear by another route."""
    from prism_service.services import design_packet as dp
    assert dp._ALLOWED_METHODS == {"owner_explicit"}, dp._ALLOWED_METHODS


def test_record_approval_refuses_a_non_owner_explicit_method():
    from prism_service.services import design_packet as dp

    class _T:
        id = "t-neg"
        plan_doc = "doc"
        plan_diagram = ""

    with pytest.raises(ValueError):
        dp.record_approval("default", "t-neg", _T(),
                           approver="owner@example.com",
                           method="browser_oracle")


def test_machine_adjudication_of_root_plan_gate_refuses_without_approval(
        tmp_path, monkeypatch):
    """NEGATIVE guard mirroring test_resweep_adjudicate_rubric_gate_
    withholds_without_approval (test_design_packet_plan_gate.py): a rubric-
    verified ROOT plan_gate task with NO design-packet approval on file must
    still be withheld by the machine adjudicator seat."""
    client, task_svc, cond, owner = _wired(tmp_path, monkeypatch)
    task = _plan_gate_task(task_svc)
    monkeypatch.setattr(
        cond, "_verify_rubric_gate",
        lambda t, validation: {"verified": True, "reason": "stub green",
                               "verifier": None, "validation": validation})

    res = cond.adjudicate_rubric_gate(task.id)

    assert res is None, res
    live = task_svc.get(task.id)
    assert live.workflow_step == "plan_gate"
    assert live.gate_state == "pending"
