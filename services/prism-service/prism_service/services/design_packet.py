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
