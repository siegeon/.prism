"""GH #181 — the supervisor must NOT fire a competing recovery during an
auto-update restart (AC-5).

The out-of-process supervisor probes ONLY the UI port. During the auto-update
re-exec the UI hangs (drain + cold boot); without a guard the supervisor would
SIGKILL + respawn the worker and stack a SECOND daemon family — the split-brain.
A fresh restart sentinel makes it DEFER; a stale/cleared sentinel lets normal
wedge recovery resume.
"""

from __future__ import annotations

import pytest

from prism_service.services import supervisor as sup


@pytest.fixture
def no_grace_state(monkeypatch):
    # Disable the unrelated cold-boot startup grace so we isolate the restart
    # sentinel behaviour, and stop real kills/respawns from ever running.
    monkeypatch.setattr(sup, "_in_startup_grace", lambda state: False)
    monkeypatch.setattr(sup, "failure_threshold", lambda: 3)
    fired = {"kills": 0, "respawns": 0}
    monkeypatch.setattr(sup, "force_kill", lambda pid: fired.__setitem__("kills", fired["kills"] + 1))

    def _respawn():
        fired["respawns"] += 1
        return 999

    monkeypatch.setattr(sup, "respawn_server", _respawn)
    state = sup.SupervisorState(server_pid=123)
    state.spawned_at = None
    return state, fired


def test_hung_probes_during_restart_do_not_recover(no_grace_state, monkeypatch):
    state, fired = no_grace_state
    monkeypatch.setattr(sup, "_restart_in_progress", lambda: True)  # fresh sentinel
    # Far more hung probes than the failure threshold — must NEVER recover.
    for _ in range(10):
        assert sup.handle_probe_result(state, alive=False) is False
    assert fired["respawns"] == 0
    assert fired["kills"] == 0
    assert state.consecutive_failures == 0   # streak not even accrued


def test_recovery_resumes_when_no_restart_in_progress(no_grace_state, monkeypatch):
    state, fired = no_grace_state
    monkeypatch.setattr(sup, "_restart_in_progress", lambda: False)  # no sentinel
    recovered = False
    for _ in range(3):                        # threshold == 3
        recovered = sup.handle_probe_result(state, alive=False) or recovered
    assert recovered is True
    assert fired["respawns"] == 1
    assert fired["kills"] == 1


def test_dead_process_not_respawned_during_restart(no_grace_state, monkeypatch):
    # A worker mid os.execv re-exec must not be respawned over (handle_liveness).
    state, fired = no_grace_state
    monkeypatch.setattr(sup, "_restart_in_progress", lambda: True)
    assert sup.handle_liveness(state, pid_alive=False) is False
    assert fired["respawns"] == 0
