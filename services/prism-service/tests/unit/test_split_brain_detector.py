"""GH #181 — split-brain detector + healer + restart sentinel (unit).

Healthy = ONE pid owns BOTH service ports. Split-brain = two DISTINCT pids each
owning one port (the orphaned old daemon squatting :7777 while the new one owns
:7778). These pin detection (conservative — never a FALSE split), the
SIGTERM->SIGKILL escalation the orphan forced (it ignored SIGTERM), and the
TTL-bounded restart sentinel that stops the supervisor's competing recovery.
"""

from __future__ import annotations

import os

import pytest

from prism_service.services import split_brain as sb
from prism_service import data_dir


# ── port_owner_pid: never raises, None on bad input ──────────────────────────
@pytest.mark.parametrize("bad", [0, -1, "x", None])
def test_port_owner_pid_bad_input_is_none(bad):
    assert sb.port_owner_pid(bad) is None


# ── detect_split_brain ───────────────────────────────────────────────────────
def _owner_map(mapping):
    return lambda port: mapping.get(int(port))


def test_single_owner_both_ports_is_healthy():
    rep = sb.detect_split_brain(7778, 7777, owner=_owner_map({7778: 100, 7777: 100}))
    assert rep.healthy is True
    assert rep.orphan_pid is None


def test_two_owners_one_per_port_is_split_brain():
    rep = sb.detect_split_brain(7778, 7777, owner=_owner_map({7778: 26138, 7777: 49019}))
    assert rep.healthy is False
    # No pidfile hint: the UI responder is canonical, MCP squatter is the orphan.
    assert rep.orphan_pid == 49019
    assert "SPLIT-BRAIN" in rep.reason


def test_pidfile_pid_decides_the_keeper():
    # When the pidfile names the MCP owner, the UI owner is the orphan instead.
    rep = sb.detect_split_brain(
        7778, 7777, pidfile_pid=49019, owner=_owner_map({7778: 26138, 7777: 49019}))
    assert rep.healthy is False
    assert rep.orphan_pid == 26138


def test_one_port_unowned_is_not_split_brain():
    # A down/mid-boot daemon (one port unowned) must NOT false-positive (NFR-1).
    rep = sb.detect_split_brain(7778, 7777, owner=_owner_map({7778: 100, 7777: None}))
    assert rep.healthy is True
    assert rep.orphan_pid is None


def test_both_ports_unowned_is_healthy():
    rep = sb.detect_split_brain(7778, 7777, owner=_owner_map({}))
    assert rep.healthy is True


# ── kill_pid_escalating: SIGTERM then SIGKILL when ignored (AC-4) ─────────────
def test_escalates_to_sigkill_when_sigterm_ignored():
    sent = []
    sb.kill_pid_escalating(
        4321, term_timeout_s=0.0, _win=False,
        _kill=lambda pid, sig: sent.append(sig),
        _alive=lambda pid: True,          # never dies on SIGTERM
        _sleep=lambda s: None,
    )
    assert sent == [sb._SIGTERM, sb._SIGKILL]


def test_no_sigkill_when_sigterm_works():
    sent = []
    sb.kill_pid_escalating(
        4321, term_timeout_s=5.0, _win=False,
        _kill=lambda pid, sig: sent.append(sig),
        _alive=lambda pid: False,         # died on SIGTERM
        _sleep=lambda s: None,
    )
    assert sent == [sb._SIGTERM]


def test_windows_is_single_taskkill():
    sent = []
    sb.kill_pid_escalating(4321, _win=True, _kill=lambda pid, sig: sent.append(sig))
    assert sent == [sb._TASKKILL]


# ── heal_split_brain reaps the orphan, no-op when healthy (AC-3) ──────────────
def test_heal_reaps_orphan():
    killed = []
    rep = sb.detect_split_brain(7778, 7777, owner=_owner_map({7778: 26138, 7777: 49019}))
    reaped = sb.heal_split_brain(rep, killer=lambda pid, t: killed.append(pid))
    assert reaped == 49019
    assert killed == [49019]


def test_heal_healthy_is_noop():
    killed = []
    rep = sb.detect_split_brain(7778, 7777, owner=_owner_map({7778: 100, 7777: 100}))
    assert sb.heal_split_brain(rep, killer=lambda pid, t: killed.append(pid)) is None
    assert killed == []


# ── restart sentinel: fresh vs stale (NFR-2) ─────────────────────────────────
@pytest.fixture
def data_dir_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    return tmp_path


def test_restart_sentinel_fresh_then_cleared(data_dir_tmp):
    assert data_dir.restart_in_progress() is False        # absent
    data_dir.write_restart_sentinel(1234)
    assert data_dir.restart_in_progress() is True         # fresh
    data_dir.clear_restart_sentinel()
    assert data_dir.restart_in_progress() is False        # cleared


def test_stale_sentinel_is_ignored(data_dir_tmp):
    data_dir.write_restart_sentinel(1234)
    # ttl_s=0 models a sentinel older than its TTL: a crash mid-restart must NOT
    # disable recovery forever (NFR-2).
    assert data_dir.restart_in_progress(ttl_s=0.0) is False
