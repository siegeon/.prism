"""Server-assembled DecisionPacket for the single-sign-off approval panel.

Task a1e4120f (slices 2 + 4 of the design in 7cc4f0cf). The approval gate must
present a COMPELLING, server-assembled evidence packet - never a bare reason
box or the "No recorded evidence for this step yet" fallback. Every field here
is read from a REAL artifact (git diff/log in the task worktree, the oracle
EvidenceReceipt, the evidence screenshot dir); nothing is client-typed.

Kept OUT of the gate-POLICY set on purpose (control_plane.POLICY_FILES): this
is a read-only VIEW over artifacts a graded task produces, so a candidate may
build it without editing its own judge.
"""
from __future__ import annotations

import subprocess
from typing import Optional

from prism_service.services import task_workspace, oracle_spec
from prism_service.data_dir import evidence_dir

_IMG = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _run_git(path: str, *args: str) -> str:
    """Best-effort git stdout in ``path``; "" on any failure (never raises)."""
    try:
        r = subprocess.run(["git", *args], cwd=path, capture_output=True,
                           text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def packet_state(workflow_step: str, gate_state: str, status: str,
                 gate_reason: str) -> str:
    """AC-2: derive the card state from REAL task fields (pure, unit-tested).
    One of pending / failed / recovered / waived / done."""
    gs = (gate_state or "").lower()
    st = (status or "").lower()
    reason = (gate_reason or "").lower()
    if st == "done" and gs == "passed":
        return "done"
    if gs == "failed":
        return "failed"
    if any(k in reason for k in ("rewound", "rewind", "recover")):
        return "recovered"
    if any(k in reason for k in ("waiv", "override")):
        return "waived"
    return "pending"


def _diff_stat(path: str, baseline: str) -> dict:
    """AC-1: diff stat vs baseline from `git diff --numstat baseline..HEAD`."""
    out = {"files": 0, "insertions": 0, "deletions": 0, "entries": []}
    if not (path and baseline):
        return out
    for line in _run_git(path, "diff", "--numstat",
                         f"{baseline}..HEAD").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        add, dele, fpath = parts
        ai = int(add) if add.isdigit() else 0
        di = int(dele) if dele.isdigit() else 0
        out["files"] += 1
        out["insertions"] += ai
        out["deletions"] += di
        out["entries"].append({"path": fpath, "insertions": ai,
                               "deletions": di})
    return out


def _commits(path: str, baseline: str) -> list:
    """AC-1: commits from `git log baseline..HEAD` (unit-sep %h/%s)."""
    if not (path and baseline):
        return []
    raw = _run_git(path, "log", "--pretty=%h\x1f%s", f"{baseline}..HEAD")
    commits = []
    for line in raw.splitlines():
        if "\x1f" in line:
            sha, subj = line.split("\x1f", 1)
            commits.append({"sha": sha, "subject": subj})
    return commits


def _receipt(project: str, task_id: str) -> Optional[dict]:
    """The latest oracle EvidenceReceipt, projected to the packet fields."""
    try:
        r = oracle_spec.latest_receipt(project, task_id)
    except Exception:
        r = None
    if r is None:
        return None
    return {"adapter": getattr(r, "adapter", ""),
            "passed": bool(getattr(r, "passed", False)),
            "status": getattr(r, "status", ""),
            "ended_at": getattr(r, "ended_at", ""),
            "reason": str(getattr(r, "reason", ""))[:300]}


def _screenshots(task_id: str) -> list:
    """Evidence images for the task, as servable /evidence urls."""
    shots = []
    try:
        for p in sorted(evidence_dir(task_id).glob("*")):
            if p.is_file() and p.suffix.lower() in _IMG:
                shots.append({"name": p.name,
                              "url": f"/api/tasks/{task_id}/evidence/{p.name}"})
    except Exception:
        pass
    return shots


def _subject(task, workflow_step: str) -> Optional[dict]:
    """Task 31c345b7 AC-1/2/3: the story/plan text THIS gate is deciding on.
    story_gate and plan_gate judge task.plan_doc (+ plan_diagram at
    plan_gate), and unlike the other four channels this one CAN exist before
    any code is written. None (never a fabricated stub) when both fields are
    genuinely blank."""
    plan_doc = getattr(task, "plan_doc", "") or ""
    plan_diagram = getattr(task, "plan_diagram", "") or ""
    if not plan_doc.strip() and not plan_diagram.strip():
        return None
    kind = "plan" if workflow_step == "plan_gate" else "story"
    title = "Plan under judgment" if kind == "plan" else "Story under judgment"
    return {"kind": kind, "title": title, "markdown": plan_doc,
            "diagram": plan_diagram}


def assemble_packet(project: str, task_id: str, task=None) -> dict:
    """AC-1/2/3: the full server-assembled packet. Resilient - a task with no
    worktree / receipts / evidence yields a well-formed empty packet, never
    raises."""
    ws = task_workspace.workspace_for(task_id)
    path = (ws or {}).get("path", "")
    baseline = (ws or {}).get("baseline", "")
    workflow_step = getattr(task, "workflow_step", "")
    return {
        "state": packet_state(workflow_step,
                              getattr(task, "gate_state", ""),
                              getattr(task, "status", ""),
                              getattr(task, "gate_reason", "")),
        "diff_stat": _diff_stat(path, baseline),
        "commits": _commits(path, baseline),
        "receipt": _receipt(project, task_id),
        "screenshots": _screenshots(task_id),
        # task 31c345b7: additive channel, the artifact under judgment at
        # story_gate/plan_gate. Never renamed/removed the 5 keys above.
        "subject": _subject(task, workflow_step),
    }
