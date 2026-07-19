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
