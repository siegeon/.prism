"""GH #181 (integration): an auto-update restart must leave ONE owner of both
ports — never a second daemon family.

This wires the three collaborators that together prevent the split-brain:
  * auto_updater.perform_restart writes the restart sentinel BEFORE draining,
    then os.execv re-execs IN PLACE (same pid -> single owner of both ports);
  * data_dir exposes the TTL-bounded sentinel;
  * supervisor.handle_probe_result DEFERS its competing kill+respawn while the
    sentinel is fresh (the competing recovery is what stacked the 2nd family).

os.execv is mocked so the test process is not replaced.
"""

from __future__ import annotations

import os

import pytest

from prism_service.services import auto_updater as au
from prism_service.services import supervisor as sup
from prism_service import data_dir


@pytest.fixture
def data_dir_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    return tmp_path


def _reset_au():
    clear = getattr(au, "clear_restart_request", None)
    if callable(clear):
        clear()


def test_perform_restart_writes_sentinel_before_exec(data_dir_tmp, monkeypatch):
    events = []
    # Record sentinel-state AT the moment of execv to prove ordering: the
    # sentinel must already be present when the re-exec fires.
    monkeypatch.setattr(
        au.os, "execv",
        lambda path, argv: events.append(("execv", data_dir.restart_in_progress())))
    try:
        au.perform_restart(server=None, drain_timeout_s=0.0)
    finally:
        _reset_au()
    assert ("execv", True) in events, "sentinel must be set before the re-exec"
    assert data_dir.restart_in_progress() is True


def test_supervisor_defers_recovery_while_restart_sentinel_fresh(data_dir_tmp, monkeypatch):
    # A real (file-backed) fresh sentinel — not a mock — must make the
    # supervisor defer its competing recovery, so no 2nd family is spawned.
    data_dir.write_restart_sentinel(os.getpid())
    monkeypatch.setattr(sup, "_in_startup_grace", lambda state: False)
    monkeypatch.setattr(sup, "failure_threshold", lambda: 3)
    fired = {"respawns": 0}
    monkeypatch.setattr(sup, "force_kill", lambda pid: None)
    monkeypatch.setattr(sup, "respawn_server", lambda: fired.__setitem__("respawns", 1) or 999)

    state = sup.SupervisorState(server_pid=123)
    state.spawned_at = None
    for _ in range(10):
        sup.handle_probe_result(state, alive=False)
    assert fired["respawns"] == 0, "supervisor must NOT respawn during a fresh restart"

    # Once the re-exec'd server clears the sentinel (healthy boot), recovery resumes.
    data_dir.clear_restart_sentinel()
    recovered = False
    for _ in range(3):
        recovered = sup.handle_probe_result(state, alive=False) or recovered
    assert recovered is True
    assert fired["respawns"] == 1
