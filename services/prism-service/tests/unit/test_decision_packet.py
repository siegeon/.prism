"""Tests for the server-assembled DecisionPacket (task a1e4120f, slice 2/4).

The packet is assembled from REAL worktree artifacts (git diff/log vs baseline,
the oracle receipt, evidence screenshots) so the approval panel never has to
show a bare "No recorded evidence" box. These tests pin: (AC-1) the git-sourced
diff_stat + commits, (AC-2) the pure state-derivation table, and (AC-3) that a
task with no worktree yields a well-formed empty packet instead of raising.
"""
import subprocess
from types import SimpleNamespace

import pytest


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _temp_repo(tmp_path):
    """A tiny git repo: one baseline commit, then two [task] commits on top."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "base.txt").write_text("base\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "baseline")
    baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                              capture_output=True, text=True).stdout.strip()
    (tmp_path / "feature.py").write_text("x = 1\ny = 2\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "feat: add feature [task:a1e4120f]")
    (tmp_path / "base.txt").write_text("base\nmore\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "chore: tweak [task:a1e4120f]")
    return baseline


def test_packet_from_temp_repo(tmp_path, monkeypatch):
    """AC-1: diff_stat + commits come from real git, not client-typed."""
    from prism_service.services import decision_packet, task_workspace
    baseline = _temp_repo(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(tmp_path), "baseline": baseline})
    monkeypatch.setattr(decision_packet, "_receipt", lambda p, t: None)
    monkeypatch.setattr(decision_packet, "_screenshots", lambda t: [])
    task = SimpleNamespace(workflow_step="green_gate", gate_state="pending",
                           status="in_progress", gate_reason="")
    pkt = decision_packet.assemble_packet("prism", "a1e4120f", task)

    assert pkt["diff_stat"]["files"] == 2          # feature.py + base.txt
    assert pkt["diff_stat"]["insertions"] >= 3
    subjects = [c["subject"] for c in pkt["commits"]]
    assert any("add feature" in s for s in subjects)
    assert len(pkt["commits"]) == 2
    assert pkt["state"] == "pending"


@pytest.mark.parametrize("step,gate,status,reason,expected", [
    ("green_gate", "passed", "done", "", "done"),
    ("green_gate", "failed", "in_progress", "oracle red", "failed"),
    ("red_gate", "pending", "in_progress", "rewound to write_failing_tests", "recovered"),
    ("green_gate", "passed", "in_progress", "override waived by owner", "waived"),
    ("green_gate", "pending", "in_progress", "", "pending"),
])
def test_packet_state_matrix(step, gate, status, reason, expected):
    """AC-2: state derives from real task fields, one row per documented combo."""
    from prism_service.services.decision_packet import packet_state
    assert packet_state(step, gate, status, reason) == expected


def test_epic_with_a_clean_rollup_shows_the_rollup_receipt_not_a_stale_one(
    monkeypatch,
):
    """Live, 2026-08-25: an epic's gate banner read "READY - evidence
    passing" while the Decision Packet directly below it showed "Oracle
    receipt: browser - manual_evidence_required" and the pinned-tests
    check read "0/3 passing" -- both a STALE, unrelated single-task
    receipt (oracle_spec.latest_receipt is just receipts[-1], the very
    last one ever recorded for this task across every gate/attempt). The
    epic-rollup path that actually decided the gate never writes an
    EvidenceReceipt row, so the packet fell back to whatever stale row
    happened to be last, reading as "not actually ready" even though it
    genuinely was.

    Reproduces exactly that: a task with live, all-done, strong-proof
    children (epic_rollup_verdict -> True) AND a stale FAILING
    oracle_spec receipt on file. The packet must surface the rollup as
    the decisive receipt, not the stale failure."""
    from prism_service.services import decision_packet, oracle_spec, task_workspace
    from prism_service import project_context

    monkeypatch.setattr(task_workspace, "workspace_for", lambda tid: None)
    monkeypatch.setattr(decision_packet, "_screenshots", lambda t: [])
    monkeypatch.setattr(
        oracle_spec, "latest_receipt",
        lambda p, t: SimpleNamespace(
            adapter="browser", passed=False, status="manual_evidence_required",
            ended_at="2026-08-17T13:28:12Z",
            reason="browser: no loadable URL found in the oracle text"))

    children = [
        {"id": "c1", "status": "done", "completion_proof": "## real proof one"},
        {"id": "c2", "status": "done", "completion_proof": "## real proof two"},
        {"id": "c3", "status": "cancelled", "completion_proof": ""},
    ]
    fake_task_svc = SimpleNamespace(
        list=lambda parent_id=None, **kw: children if parent_id == "epic-1" else [])
    monkeypatch.setattr(project_context, "get_project",
                        lambda project: SimpleNamespace(task_svc=fake_task_svc))

    task = SimpleNamespace(workflow_step="green_gate", gate_state="pending",
                           status="in_progress", gate_reason="")
    pkt = decision_packet.assemble_packet("prism", "epic-1", task)

    assert pkt["receipt"]["adapter"] == "epic-rollup", pkt["receipt"]
    assert pkt["receipt"]["passed"] is True, pkt["receipt"]
    assert "manual_evidence_required" not in str(pkt["receipt"]), pkt["receipt"]
    assert "browser" not in pkt["receipt"]["adapter"], pkt["receipt"]


def test_a_task_with_no_children_still_falls_back_to_its_own_receipt(monkeypatch):
    """Regression guard: a normal (non-epic) task's own receipt path must
    stay exactly as before -- the rollup lookup finds no live children and
    gets out of the way."""
    from prism_service.services import decision_packet, oracle_spec, task_workspace
    from prism_service import project_context

    monkeypatch.setattr(task_workspace, "workspace_for", lambda tid: None)
    monkeypatch.setattr(decision_packet, "_screenshots", lambda t: [])
    monkeypatch.setattr(
        oracle_spec, "latest_receipt",
        lambda p, t: SimpleNamespace(
            adapter="pytest_ids", passed=True, status="passed",
            ended_at="2026-08-25T00:00:00Z", reason="3 passed"))
    fake_task_svc = SimpleNamespace(list=lambda parent_id=None, **kw: [])
    monkeypatch.setattr(project_context, "get_project",
                        lambda project: SimpleNamespace(task_svc=fake_task_svc))

    task = SimpleNamespace(workflow_step="green_gate", gate_state="pending",
                           status="in_progress", gate_reason="")
    pkt = decision_packet.assemble_packet("prism", "leaf-task-1", task)

    assert pkt["receipt"]["adapter"] == "pytest_ids", pkt["receipt"]
    assert pkt["receipt"]["passed"] is True, pkt["receipt"]


def test_diff_and_commits_resolve_a_fresh_baseline_not_the_stale_stored_one(
    tmp_path, monkeypatch,
):
    """Live, 2026-08-28 (task bb388e9d): the Evidence tab showed "Diff vs
    baseline +52404 -2078, 292 files" and "413 commits" for a candidate whose
    REAL diff (against a freshly-resolved merge-base) was 3 files -- because
    assemble_packet read ws["baseline"] verbatim, a value set ONCE at
    workspace creation and never updated as origin/main moved forward. The
    packet must resolve the SAME forward-corrected baseline the gate-policy
    tooth already trusts (control_plane.resolve_fresh_baseline), never the
    raw stored one."""
    from prism_service.services import decision_packet, task_workspace, control_plane
    baseline = _temp_repo(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(tmp_path), "baseline": baseline})
    monkeypatch.setattr(decision_packet, "_receipt", lambda p, t: None)
    monkeypatch.setattr(decision_packet, "_screenshots", lambda t: [])

    calls = []

    def _fake_resolve(path, stale):
        calls.append((path, stale))
        return baseline  # forward-corrected value the real function would pick

    monkeypatch.setattr(control_plane, "resolve_fresh_baseline", _fake_resolve)
    task = SimpleNamespace(workflow_step="green_gate", gate_state="pending",
                           status="in_progress", gate_reason="")
    decision_packet.assemble_packet("prism", "a1e4120f", task)

    assert calls == [(str(tmp_path), baseline)], (
        "assemble_packet must resolve the baseline through "
        "control_plane.resolve_fresh_baseline before diffing, passing the "
        "workspace path and the RAW stored baseline — never diff against "
        "the stored baseline directly"
    )


def test_empty_when_no_worktree(monkeypatch):
    """AC-3: no registered worktree -> well-formed empty packet, never raises."""
    from prism_service.services import decision_packet, task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for", lambda tid: None)
    monkeypatch.setattr(decision_packet, "_receipt", lambda p, t: None)
    monkeypatch.setattr(decision_packet, "_screenshots", lambda t: [])
    task = SimpleNamespace(workflow_step="green_gate", gate_state="pending",
                           status="in_progress", gate_reason="")
    pkt = decision_packet.assemble_packet("prism", "nonexistent-task", task)

    assert pkt["diff_stat"]["files"] == 0
    assert pkt["commits"] == []
    assert pkt["receipt"] is None
    assert pkt["screenshots"] == []
    assert pkt["state"] == "pending"
