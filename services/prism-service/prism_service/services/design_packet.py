"""Server-assembled DESIGN PACKET, approved like an evidence package.

Task c016667f. plan_gate was scored ONLY by arc_governance.score_plan_coverage
(a pure text rubric) with zero owner-approval concept. This module binds
plan_diagram + plan_doc + prototype file bytes + task.oracle + task.likely_
misfire into one addressable, content-hashed object and records an explicit
owner APPROVAL as an append-only receipt (approver + sha256 content hash +
timestamp) - mirroring oracle_spec.EvidenceReceipt's append-only JSONL shape.

Kept OUT of control_plane.POLICY_FILES on purpose: like decision_packet.py,
this is read-only assembly over artifacts a task already produced, plus a
small approval ledger - not gate-scoring logic itself.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from prism_service import data_dir

# Only an explicit, distinct owner write action counts as approval (FR-7).
# A render-only / browser-oracle receipt proving the prototype merely
# LOADED is not owner sign-off (the 7cc4f0cf friction) - so it is never in
# this set, whatever string a caller sends.
_ALLOWED_METHODS = {"owner_explicit"}


def prototype_bytes_for(task_id: str) -> bytes:
    """The task's live prototype file bytes, or b"" when none exists yet."""
    p = data_dir.prototype_file(task_id)
    try:
        return p.read_bytes() if p.exists() else b""
    except OSError:
        return b""


def _content_hash(plan_doc: str, plan_diagram: str, proto_bytes: bytes) -> str:
    """sha256 over all THREE parts (AC-9: any one changing changes this),
    mirroring OracleSpec.spec_hash's canonical-blob-then-hash shape."""
    h = hashlib.sha256()
    h.update((plan_doc or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((plan_diagram or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(proto_bytes or b"")
    return "sha256:" + h.hexdigest()


def _hash_for_task(task_id: str, task) -> str:
    proto_bytes = prototype_bytes_for(task_id)
    return _content_hash(getattr(task, "plan_doc", "") or "",
                         getattr(task, "plan_diagram", "") or "",
                         proto_bytes)


def order_report(task, proto_bytes: bytes, plan_diagram: str) -> dict:
    """FR-2: the HIGHEST ORDER of visual plan the feature admits vs. what
    the packet actually has, unconditionally (AC-10: applies to every task,
    diagram-only backend/defect/refactor slices included - the "ui" tag only
    selects what a UI feature ADMITS, never whether the park rule applies)."""
    tags = set(getattr(task, "tags", None) or [])
    admits = "prototype" if "ui" in tags else "diagram"
    if proto_bytes:
        actual = "prototype"
    elif (plan_diagram or "").strip():
        actual = "diagram"
    else:
        actual = "none"
    below_order = admits == "prototype" and actual != "prototype"
    return {"admits": admits, "actual": actual, "below_order": below_order}


def assemble_packet(project: str, task_id: str, task=None) -> dict:
    """FR-1: bind plan_diagram + plan_doc + prototype bytes + oracle +
    likely_misfire into ONE addressable object, plus FR-2's order report and
    the content hash approval is recorded against. ``project`` is accepted
    for symmetry with decision_packet.assemble_packet / the approval ledger
    below even though the packet's own parts are task-scoped, not
    project-scoped (mirrors prototype_file's global-by-task-id storage)."""
    proto_bytes = prototype_bytes_for(task_id)
    plan_doc = getattr(task, "plan_doc", "") or ""
    plan_diagram = getattr(task, "plan_diagram", "") or ""
    return {
        "task_id": task_id,
        "project": project,
        "plan_doc": plan_doc,
        "plan_diagram": plan_diagram,
        "oracle": getattr(task, "oracle", "") or "",
        "likely_misfire": getattr(task, "likely_misfire", "") or "",
        "prototype": {"exists": bool(proto_bytes),
                     "bytes": len(proto_bytes)},
        "order": order_report(task, proto_bytes, plan_diagram),
        "content_hash": _content_hash(plan_doc, plan_diagram, proto_bytes),
    }


# ---------------------------------------------------------------------------
# FR-6/FR-7 - append-only owner-approval receipt (mirrors oracle_spec's
# EvidenceReceipt / _receipts_path / append_receipt shape).
# ---------------------------------------------------------------------------


@dataclass
class Approval:
    task_id: str
    approver: str
    method: str
    packet_hash: str
    approved_at: str

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "approver": self.approver,
                "method": self.method, "packet_hash": self.packet_hash,
                "approved_at": self.approved_at}

    @classmethod
    def from_dict(cls, d: dict) -> "Approval":
        return cls(task_id=d.get("task_id", ""), approver=d.get("approver", ""),
                   method=d.get("method", ""),
                   packet_hash=d.get("packet_hash", ""),
                   approved_at=d.get("approved_at", ""))


def _approvals_path(project: str, task_id: str) -> Path:
    from prism_service.config import project_data_dir
    d = project_data_dir(project or "default") / "design_approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{task_id}.jsonl"


def read_approvals(project: str, task_id: str) -> list:
    path = _approvals_path(project, task_id)
    if not path.exists():
        return []
    out: list = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Approval.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def latest_approval(project: str, task_id: str) -> Optional[Approval]:
    approvals = read_approvals(project, task_id)
    return approvals[-1] if approvals else None


def record_approval(project: str, task_id: str, task, approver: str,
                    method: str) -> Approval:
    """FR-6/FR-7: append ONE owner-approval receipt. Never overwrites prior
    history (AC append-only) - a re-approval after an edit adds a new row at
    the NEW content hash. Raises ValueError on a missing approver identity
    or a non-explicit method (AC-8: a render/browser receipt is refused)."""
    approver = (approver or "").strip()
    if not approver:
        raise ValueError("design-packet approval requires an approver identity")
    if method not in _ALLOWED_METHODS:
        raise ValueError(
            f"design-packet approval method {method!r} is not an explicit "
            "owner action - a render-only/browser receipt proves the "
            "prototype loaded, not that the owner approved it")
    appr = Approval(task_id=task_id, approver=approver, method=method,
                    packet_hash=_hash_for_task(task_id, task),
                    approved_at=datetime.now(timezone.utc).isoformat())
    path = _approvals_path(project, task_id)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(appr.as_dict(), separators=(",", ":")) + "\n")
    return appr


def approval_status(project: str, task_id: str, task) -> dict:
    """FR-4/FR-5/FR-8: {approved, stale, reason} recomputed on READ every
    time (never a stored flag) against the packet's CURRENT content hash -
    so editing plan_doc/plan_diagram/prototype after approval is caught here,
    not by a flag someone forgot to clear.

    ROOT TASKS ONLY. Owner 2026-08-06: "the children sub-tasks can not be
    blocked, as only you can do the work, and you need to validate sub
    tasks, you only involve me at parent task level."

    A child slice belongs to the driver, so waiting there waits on someone
    who never intended to look at it. Three children of epic 0784729f sat on
    "no approval is on file yet" while that epic's progress - counted in
    children DONE - stayed at 7/13 through a dozen commits of finished work.
    Root tasks are untouched and still park below.

    This changes WHO IS ASKED, never who may answer: `record_approval` still
    accepts only `owner_explicit`, so no driver can record a sign-off in the
    owner's name.
    """
    if str(getattr(task, "parent_id", "") or "").strip():
        return {"approved": True, "stale": False, "reason": ""}

    latest = latest_approval(project, task_id)
    if latest is None:
        return {"approved": False, "stale": False, "reason": (
            "plan_gate needs an explicit owner approval of the design "
            "packet (plan_doc + plan_diagram + prototype) before it can "
            "clear - no approval is on file yet")}
    current_hash = _hash_for_task(task_id, task)
    if latest.packet_hash != current_hash:
        return {"approved": False, "stale": True, "reason": (
            "the design packet changed since it was approved "
            f"(approved {latest.packet_hash[:19]}..., now "
            f"{current_hash[:19]}...) - re-approval is required")}
    return {"approved": True, "stale": False, "reason": ""}


# ---------------------------------------------------------------------------
# Task 594f9a58 - the adjudicator minds the root plan gate.
#
# Owner 2026-08-30: "we moved from here. if we have to escalate p90
# certainty or whatever then you can block on the gate, but the ontology
# and adjudicator is to mind the gates, we made it a game."
#
# BEFORE this: a ROOT task's plan_gate could clear ONLY through the owner's
# own POST /api/conductor/design-packet/approve (method="owner_explicit"),
# so a strong, obviously-ready design packet still waited on a human click
# forever. This section gives the adjudicator seat a REAL, non-constant
# certainty score over the packet itself, and lets it decide the gate on
# its own authority once that score clears a configurable threshold -
# never by widening _ALLOWED_METHODS or calling record_approval, so
# task 98d38111's rule (no driver signs as the owner) stays intact: a
# certainty-approve is attributed to ADJUDICATOR_SEAT on the gate_decide
# history row, never to an "owner_explicit" ledger entry.
# ---------------------------------------------------------------------------

DEFAULT_CERTAINTY_THRESHOLD = 0.90

# Floors the four signals below are scored against - each is a REAL,
# independently-computable measure of the packet's own content (AC-1's
# likely_misfire guard: no branch below ever returns a fixed literal for
# the whole score).
_PLAN_WORD_FLOOR = 150
_ORACLE_CHAR_FLOOR = 40


def certainty_threshold() -> float:
    """PRISM_PLAN_GATE_CERTAINTY_THRESHOLD, defaulting to 0.90 on an unset,
    unparsable, or out-of-[0,1] value - mirrors gate_adjudicator._interval_s's
    exact degrade-to-default shape."""
    raw = os.environ.get("PRISM_PLAN_GATE_CERTAINTY_THRESHOLD", "")
    try:
        v = float(raw) if raw.strip() else DEFAULT_CERTAINTY_THRESHOLD
    except ValueError:
        return DEFAULT_CERTAINTY_THRESHOLD
    return v if 0.0 <= v <= 1.0 else DEFAULT_CERTAINTY_THRESHOLD


def plan_gate_certainty(project: str, task_id: str, task) -> dict:
    """Real, multi-signal certainty score (0..1) for the adjudicator seat
    deciding a root plan_gate with no owner_explicit approval on file.
    Every signal is computed from the design packet's own content - never
    a constant - so a thin or under-scoped packet genuinely scores low and
    a rich one genuinely scores high (AC-1).

    Four signals, unweighted average:
      - plan_completeness: plan_doc word count against a floor.
      - oracle_quality: oracle + likely_misfire each carry real substance,
        not boilerplate or empty text.
      - diagram_quality: plan_diagram parses AND carries more than one
        trivial edge.
      - scope_alignment: order_report's existing below_order check - a
        ui-tagged feature is not below the order it admits.
    """
    plan_doc = getattr(task, "plan_doc", "") or ""
    oracle = getattr(task, "oracle", "") or ""
    misfire = getattr(task, "likely_misfire", "") or ""
    diagram = getattr(task, "plan_diagram", "") or ""

    words = len(plan_doc.split())
    plan_completeness = min(1.0, words / float(_PLAN_WORD_FLOOR))

    oracle_len = len(oracle.strip())
    misfire_len = len(misfire.strip())
    if oracle_len >= _ORACLE_CHAR_FLOOR and misfire_len >= _ORACLE_CHAR_FLOOR:
        oracle_quality = 1.0
    elif oracle_len and misfire_len:
        oracle_quality = 0.5
    else:
        oracle_quality = 0.0

    from prism_service.services.arc_governance import (
        mermaid_parses, mermaid_edges)
    if diagram.strip() and mermaid_parses(diagram):
        edge_count = len(mermaid_edges(diagram))
        diagram_quality = 1.0 if edge_count >= 2 else 0.5
    else:
        diagram_quality = 0.0

    proto_bytes = prototype_bytes_for(task_id)
    order = order_report(task, proto_bytes, diagram)
    scope_alignment = 0.0 if order.get("below_order") else 1.0

    signals = {
        "plan_completeness": round(plan_completeness, 3),
        "oracle_quality": round(oracle_quality, 3),
        "diagram_quality": round(diagram_quality, 3),
        "scope_alignment": round(scope_alignment, 3),
    }
    score = round(sum(signals.values()) / len(signals), 3)

    reasons: list[str] = []
    if plan_completeness < 1.0:
        reasons.append(
            f"plan_doc is thin ({words} word(s) against a "
            f"{_PLAN_WORD_FLOOR}-word floor)")
    if oracle_quality < 1.0:
        reasons.append(
            "oracle and/or likely_misfire is missing or too short "
            f"(< {_ORACLE_CHAR_FLOOR} chars each) for a human to judge "
            "without reading the source")
    if diagram_quality < 1.0:
        reasons.append(
            "plan_diagram is missing, does not parse, or carries fewer "
            "than two edges")
    if scope_alignment < 1.0:
        reasons.append(
            "the design packet is below the order the task admits (a "
            "ui-tagged feature with no prototype on file)")
    return {"score": score, "signals": signals, "reasons": reasons}


def root_plan_gate_escalation_reason(project: str, task_id: str, task,
                                     status: dict) -> str:
    """The ONE string both the certainty seat (adjudicate_root_plan_gate)
    and the decline-reason surfacer (gate_adjudicator._pending_decline_
    reason) report for a root plan_gate that certainty does not clear -
    computed fresh each time from the SAME plan_gate_certainty call, so the
    two call sites can never disagree or race each other's gate_reason
    write (AC-3: a human always sees a concrete reason, never a generic
    placeholder once a certainty score exists)."""
    certainty = plan_gate_certainty(project, task_id, task)
    reasons = certainty.get("reasons") or []
    if not reasons:
        return str(status.get("reason") or "") or (
            "Plan rubric passed (machine review). Your approval releases "
            "the plan.")
    threshold = certainty_threshold()
    return (
        f"escalating to you - design-packet certainty {certainty['score']:.2f} "
        f"is below the {threshold:.2f} threshold: " + "; ".join(reasons))


def _root_conductor_plan_gate(task) -> bool:
    """True for a ROOT task (parent_id empty) on the real conductor/
    implement SDLC - the only shape the owner's plan-gate stop (task
    3c774abd) ever applies to. Duplicated from api/conductor_flow.py's
    ``_is_root_conductor_task`` rather than imported: this is a services
    module and conductor_flow is an api module, so importing it here would
    invert the layering (services -> api is forbidden, ARC-PRISM-2). Both
    copies check the exact same two fields and must never disagree."""
    from prism_service.models.task import normalize_workflow
    parent_id = str(getattr(task, "parent_id", "") or "").strip()
    workflow = normalize_workflow(getattr(task, "workflow", "") or "")
    return not parent_id and workflow == "implement"


def adjudicate_root_plan_gate(cond, task_id: str, task, project: str
                              ) -> Optional[dict]:
    """The adjudicator's OWN certainty-gated decision for a root plan_gate
    with no owner_explicit approval on file (task 594f9a58).

    Returns None when this seat has nothing to say - not a root/plan_gate/
    pending task, or the plan rubric itself has not verified yet - so the
    caller falls through to the ordinary rubric autoclear unchanged (a
    child task's plan_gate is completely untouched by this function).
    Returns {"ok": True, ...} on a certainty-approve (a real gate_decide
    result). Returns {"ok": False} when certainty ran and this seat parked
    the gate with its own reason - the caller must NOT then also run the
    generic rubric-approve path, or a second write could race the first.

    ``cond`` only needs ``.gate_decide`` and ``._task_svc`` - duck-typed so
    this module never imports ConductorService (design_packet stays
    gate-scoring-ADJACENT, per its own module docstring, never gate-scoring
    policy itself: the score is a plain function of the packet; only
    ``cond.gate_decide`` - already the shared, audited entry point every
    other adjudicator seat uses - actually moves the gate).
    """
    if task is None or getattr(task, "workflow_step", "") != "plan_gate":
        return None
    if getattr(task, "gate_state", "") != "pending":
        return None
    if not _root_conductor_plan_gate(task):
        return None

    from prism_service.models.task import normalize_workflow
    task_workflow = normalize_workflow(getattr(task, "workflow", "") or "")
    validation = cond._validation_for_gate("plan_gate", task_workflow)
    if not validation:
        return None
    check = cond._verify_rubric_gate(task, validation)
    if check.get("verified") is not True:
        return None

    status = approval_status(project, task_id, task)
    if status.get("approved"):
        return None

    try:
        from prism_service.services import control_plane as _cp
        _pr = _cp.candidate_controls_judge_reason(task)
    except Exception:
        _pr = None
    if _pr:
        try:
            cond._park_policy_abstention(task_id, "plan_gate", _pr)
        except Exception:
            pass
        return {"ok": False}

    certainty = plan_gate_certainty(project, task_id, task)
    threshold = certainty_threshold()
    if certainty["score"] >= threshold:
        from prism_service.services.conductor_service import ADJUDICATOR_SEAT
        reason = (
            f"machine adjudication (certainty): design-packet "
            f"certainty={certainty['score']:.2f} >= threshold "
            f"{threshold:.2f} "
            f"(signals: {certainty['signals']}) - owner_explicit approval "
            "remains the only way to sign in the owner's name")
        res = cond.gate_decide(
            task_id, "approve", reason=reason,
            session_id=ADJUDICATOR_SEAT, actor=ADJUDICATOR_SEAT,
            model="machine")
        return res if res and res.get("ok") else {"ok": False}

    _r = root_plan_gate_escalation_reason(project, task_id, task, status)
    if _r != (getattr(task, "gate_reason", "") or ""):
        try:
            cond._task_svc.update(task_id, gate_reason=_r)
        except Exception:
            pass
    return {"ok": False}
