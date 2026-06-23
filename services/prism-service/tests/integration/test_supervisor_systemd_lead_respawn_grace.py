"""Red scaffold (integration) — supervisor survives systemd + waits for a
healthy respawn (GH #162 hardening, task d10e4623).

The shipped out-of-process supervisor (services/supervisor.py, v6.5.6) is a
SIBLING of the server: cmd_start spawns the server, writes the SERVER pid as
the systemd unit main pid, then launches the supervisor passing that pid.
Under systemd Type=simple that topology is fatal — systemd tracks the SERVER
pid, so the instant the supervisor SIGKILLs the wedged server the cgroup is
torn down and the supervisor dies with it (GAP 1). And after a (re)spawn a
cold-booting child (model load ~7-9s) that is still warming up reads as N
hung probes and gets killed BEFORE it ever became healthy (GAP 2), with
max_restarts defaulting to 0/unlimited so a genuinely crash-looping child
thrashes forever instead of backing off to systemd.

These tests pin the REAL seams (not unit contracts that pass on dead code):

  G1-LEAD    run_supervisor invoked with NO server-pid arg is LEAD mode: it
             spawns the server as its OWN child (via _spawn_server) and
             supervises it, so the LONG-LIVED parent systemd tracks is the
             SUPERVISOR, not the server.
  G1-STABLE  across a full kill+respawn cycle in lead mode the LEAD pid never
             changes while the supervised server pid DOES — the systemd unit
             main pid survives a recovery.
  G1-COMPAT  the existing `<server_pid> <ui_port>` sibling invocation still
             drives supervise_loop against that pid (backward-compat).
  G1-UNIT    a committed deploy/prism.service ExecStarts the supervisor LEAD
             mode with Restart=always.
  G2-GRACE   after a (re)spawn the supervisor waits for probe_alive OR a
             startup_grace_s (~30s, env-tunable) timeout BEFORE a hung probe
             counts toward a kill — a slow boot is NOT killed during grace.
  G2-REGRACE the startup grace is RE-APPLIED after EACH respawn, not just the
             first boot (grace runs again after a recovery cycle).
  G2-MAXDEF  max_restarts default is a FINITE window (N in M s), not 0 —
             a crash-looping child stops after the window.
  VER        PRISM_VERSION patch-bumped past 6.5.6 with GH #162 notes.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# G1-LEAD — no server-pid arg => LEAD mode: supervisor spawns the server as
# its OWN child and supervises it (systemd tracks the SUPERVISOR).
# ---------------------------------------------------------------------------
def test_lead_mode_spawns_server_as_own_child(monkeypatch):
    """`python -m prism_service.services.supervisor` with NO args is LEAD
    mode: it must spawn the server itself (via _spawn_server) and then
    supervise that child. Pins the REAL run_supervisor dispatch, not a unit
    helper — a lead-mode entry that never calls _spawn_server is dead."""
    sup = importlib.import_module("prism_service.services.supervisor")

    spawned = {"n": 0}
    monkeypatch.setattr(sup, "_spawn_server", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1) or 31337, raising=False)
    monkeypatch.setattr(sup, "_write_pid", lambda *a, **k: None, raising=False)

    captured = {}

    def _fake_loop(server_pid, port, **kwargs):
        captured["server_pid"] = server_pid
        captured["port"] = port

    monkeypatch.setattr(sup, "supervise_loop", _fake_loop, raising=False)
    monkeypatch.setenv("PRISM_SUPERVISOR", "on")

    # NO server-pid arg -> lead mode.
    rc = sup.run_supervisor([])
    assert rc == 0
    assert spawned["n"] == 1, (
        "lead mode (no server-pid arg) did NOT spawn the server as its own "
        "child — the supervisor is not the long-lived systemd parent"
    )
    assert captured.get("server_pid") == 31337, (
        "lead mode did not supervise the child it spawned"
    )


def test_lead_pid_stable_while_server_pid_changes_across_respawn(monkeypatch):
    """G1-STABLE: across a full kill+respawn cycle the supervising (LEAD)
    process NEVER exits — only the server child pid changes. We drive the
    PURE state machine through a recovery and assert the loop continues
    (returns control / does not raise SystemExit), the server pid rotated,
    and recovery fired exactly once. This is the systemd-unit-main-pid-
    survives invariant expressed at the handle_probe_result seam."""
    sup = importlib.import_module("prism_service.services.supervisor")

    monkeypatch.setattr(sup, "force_kill", lambda *a, **k: None)
    monkeypatch.setattr(sup, "respawn_server", lambda *a, **k: 99999, raising=False)
    # Grace must not swallow the recovery in this pure-machine drive.
    if hasattr(sup, "_in_startup_grace"):
        monkeypatch.setattr(sup, "_in_startup_grace", lambda *a, **k: False, raising=False)

    state = sup.SupervisorState(server_pid=11111)
    threshold = sup.failure_threshold()
    fired = False
    for _ in range(threshold):
        fired = sup.handle_probe_result(state, alive=False) or fired

    assert fired is True, "recovery never fired across the cycle"
    assert state.server_pid == 99999, (
        "server child pid did not rotate after respawn — the LEAD process "
        "must keep supervising the NEW pid"
    )


# ---------------------------------------------------------------------------
# G1-COMPAT — the existing <server_pid> <ui_port> sibling invocation still
# supervises that pid (cmd_start's proven topology must keep working).
# ---------------------------------------------------------------------------
def test_sibling_mode_still_supervises_given_pid(monkeypatch):
    """Backward-compat: `python -m ...supervisor <server_pid> <ui_port>`
    (the cmd_start sibling invocation) must STILL drive supervise_loop
    against the given pid WITHOUT spawning a new server. Adding lead mode
    must not regress the proven sibling path."""
    sup = importlib.import_module("prism_service.services.supervisor")

    spawned = {"n": 0}
    monkeypatch.setattr(sup, "_spawn_server", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1) or 7, raising=False)

    captured = {}
    monkeypatch.setattr(
        sup, "supervise_loop",
        lambda server_pid, port, **kw: captured.update(server_pid=server_pid, port=port),
        raising=False,
    )
    monkeypatch.setenv("PRISM_SUPERVISOR", "on")

    rc = sup.run_supervisor(["54321", "8888"])
    assert rc == 0
    assert spawned["n"] == 0, (
        "sibling mode must NOT spawn a new server — it supervises the pid "
        "cmd_start already started"
    )
    assert captured.get("server_pid") == 54321, "sibling mode lost the given pid"
    assert str(captured.get("port")) == "8888", "sibling mode lost the given port"


# ---------------------------------------------------------------------------
# G1-UNIT — committed deploy/prism.service runs the supervisor LEAD mode
# with Restart=always (systemd-on-Linux is a manual customer step).
# ---------------------------------------------------------------------------
def _unit_path() -> Path:
    # tests/integration/<this> -> services/prism-service/deploy/prism.service
    return Path(__file__).resolve().parents[2] / "deploy" / "prism.service"


def test_systemd_unit_file_shipped_with_lead_mode_restart_always():
    """A committed, documented services/prism-service/deploy/prism.service
    must exist; its ExecStart must invoke the supervisor LEAD mode (the
    module run with NO trailing <server_pid> arg) and it must set
    Restart=always so systemd respawns the supervisor if it ever dies."""
    unit = _unit_path()
    assert unit.exists(), (
        f"deploy/prism.service not shipped at {unit} — the systemd unit that "
        f"runs the supervisor lead mode is missing"
    )
    text = unit.read_text(encoding="utf-8")

    exec_lines = [ln for ln in text.splitlines() if ln.strip().startswith("ExecStart=")]
    assert exec_lines, "prism.service has no ExecStart= line"
    exec_line = exec_lines[0]
    assert "prism_service.services.supervisor" in exec_line, (
        "ExecStart does not launch the supervisor module (lead mode)"
    )
    # Lead mode = module run with NO numeric <server_pid> trailing arg.
    tail = exec_line.split("prism_service.services.supervisor", 1)[1]
    assert not re.search(r"\b\d{2,}\b", tail), (
        "ExecStart passes a server-pid arg — that is SIBLING mode; the unit "
        "must run LEAD mode (no server-pid) so systemd tracks the supervisor"
    )
    assert re.search(r"^\s*Restart\s*=\s*always", text, re.MULTILINE), (
        "prism.service must set Restart=always so systemd respawns the "
        "supervisor itself if it dies"
    )


# ---------------------------------------------------------------------------
# G2-GRACE — after a (re)spawn the supervisor waits for probe_alive OR a
# startup_grace_s timeout BEFORE a hung probe counts toward a kill.
# ---------------------------------------------------------------------------
def test_startup_grace_knob_exists_and_is_env_tunable(monkeypatch):
    """A startup_grace_s() knob (~30s default, env-tunable) must exist — the
    grace window long enough to cover a cold model-load boot (~7-9s) plus
    headroom, and longer than probe_interval*failure_threshold so a slow boot
    cannot be killed during it."""
    sup = importlib.import_module("prism_service.services.supervisor")
    assert hasattr(sup, "startup_grace_s"), (
        "supervisor.py exposes no startup_grace_s() knob — there is no "
        "post-respawn grace window"
    )
    monkeypatch.delenv("PRISM_SUPERVISOR_STARTUP_GRACE_S", raising=False)
    default = sup.startup_grace_s()
    assert default >= 20.0, (
        f"startup_grace_s default {default}s is too short to cover a cold "
        f"model-load boot (~7-9s) with headroom"
    )
    # The grace must outlast the kill window, else a slow boot is killed mid-grace.
    assert default > sup.probe_interval_s() * sup.failure_threshold(), (
        "startup grace must be longer than the recovery bound, else a slow "
        "boot is killed before it can become healthy"
    )
    monkeypatch.setenv("PRISM_SUPERVISOR_STARTUP_GRACE_S", "12.5")
    assert sup.startup_grace_s() == 12.5, "startup_grace_s is not env-tunable"


def test_slow_boot_not_killed_during_startup_grace(monkeypatch):
    """A child whose boot exceeds probe_interval*failure_threshold is NOT
    killed before startup_grace_s elapses: hung probes that arrive WHILE the
    fresh child is still inside its startup grace must NOT count toward a
    kill. Pins handle_probe_result honoring the grace window."""
    sup = importlib.import_module("prism_service.services.supervisor")

    killed = {"n": 0}
    monkeypatch.setattr(sup, "force_kill", lambda *a, **k: killed.__setitem__("n", killed["n"] + 1))
    monkeypatch.setattr(sup, "respawn_server", lambda *a, **k: 2, raising=False)

    # A freshly (re)spawned child, still inside its startup grace.
    state = sup.SupervisorState(server_pid=12345)
    assert hasattr(state, "spawned_at") or hasattr(state, "grace_until") or hasattr(state, "spawn_monotonic"), (
        "SupervisorState tracks no spawn time — handle_probe_result cannot "
        "know whether the child is still inside its startup grace"
    )
    # Force "we are inside the startup grace" deterministically.
    monkeypatch.setattr(sup, "_in_startup_grace", lambda st: True, raising=False)

    # Many more hung probes than the failure threshold, all during grace.
    for _ in range(sup.failure_threshold() * 3):
        fired = sup.handle_probe_result(state, alive=False)
        assert fired is False, "recovery fired while the child was inside its startup grace"
    assert killed["n"] == 0, (
        "a slow-booting child was force_killed during its startup grace — "
        "the grace window does not gate the kill"
    )


def test_grace_reapplied_after_each_respawn(monkeypatch):
    """G2-REGRACE: the startup grace must be RE-EVALUATED after EACH respawn,
    not only on first boot. After a recovery cycle rotates the server pid,
    the fresh child gets a fresh grace window — so a (re)spawn followed by
    hung-during-grace probes still does NOT trigger a second kill."""
    sup = importlib.import_module("prism_service.services.supervisor")

    kills = {"n": 0}
    monkeypatch.setattr(sup, "force_kill", lambda *a, **k: kills.__setitem__("n", kills["n"] + 1))
    monkeypatch.setattr(sup, "respawn_server", lambda *a, **k: 777, raising=False)

    state = sup.SupervisorState(server_pid=111)

    # Phase 1: NOT in grace -> the first kill+respawn fires after threshold.
    monkeypatch.setattr(sup, "_in_startup_grace", lambda st: False, raising=False)
    for _ in range(sup.failure_threshold()):
        sup.handle_probe_result(state, alive=False)
    assert kills["n"] == 1, "first recovery cycle did not fire"
    assert state.server_pid == 777, "server pid did not rotate on respawn"

    # The respawn must have refreshed the grace anchor.
    grace_anchor = (
        getattr(state, "spawned_at", None)
        or getattr(state, "grace_until", None)
        or getattr(state, "spawn_monotonic", None)
    )
    assert grace_anchor is not None, (
        "respawn did not re-stamp the startup-grace anchor on the state — "
        "grace is not re-applied per respawn"
    )

    # Phase 2: the FRESH child is now in grace again -> hung probes during
    # this second grace window must NOT trigger another kill.
    monkeypatch.setattr(sup, "_in_startup_grace", lambda st: True, raising=False)
    for _ in range(sup.failure_threshold() * 2):
        sup.handle_probe_result(state, alive=False)
    assert kills["n"] == 1, (
        "a second kill fired during the RE-APPLIED post-respawn grace — the "
        "grace is only honored on first boot, not per respawn"
    )


# ---------------------------------------------------------------------------
# G2-MAXDEF — max_restarts default is a FINITE window, not 0/unlimited; a
# crash-looping child stops after the window instead of thrashing forever.
# ---------------------------------------------------------------------------
def test_max_restarts_default_is_finite(monkeypatch):
    """The thundering-herd / crash-loop guard must DEFAULT to a finite window
    (so a genuinely crash-looping fresh child backs off to systemd) instead
    of the old 0=unlimited. With no env override, max_restarts() must be a
    small finite N, not 0."""
    sup = importlib.import_module("prism_service.services.supervisor")
    monkeypatch.delenv("PRISM_SUPERVISOR_MAX_RESTARTS", raising=False)
    cap = sup.max_restarts()
    assert isinstance(cap, int) and cap > 0, (
        f"max_restarts default is {cap} (0/unlimited) — a crash-looping child "
        f"thrashes forever; it must default to a FINITE window"
    )
    assert cap <= 100, f"max_restarts default {cap} is implausibly large for a backoff window"


def test_crash_looping_child_stops_after_finite_window(monkeypatch):
    """A child that wedges immediately on every respawn must STOP being
    respawned once the finite max_restarts window is exhausted (it backs off
    to systemd / leaves the server down) rather than thrashing forever."""
    sup = importlib.import_module("prism_service.services.supervisor")

    respawns = {"n": 0}

    def _respawn(*a, **k):
        respawns["n"] += 1
        return 1000 + respawns["n"]

    monkeypatch.setattr(sup, "force_kill", lambda *a, **k: None)
    monkeypatch.setattr(sup, "respawn_server", _respawn, raising=False)
    # Never inside grace, so every threshold-run trips a kill+respawn.
    monkeypatch.setattr(sup, "_in_startup_grace", lambda st: False, raising=False)

    monkeypatch.delenv("PRISM_SUPERVISOR_MAX_RESTARTS", raising=False)
    cap = sup.max_restarts()
    assert cap > 0, "precondition: finite default cap"

    state = sup.SupervisorState(server_pid=500)
    # Drive far more recovery cycles than the cap.
    for _ in range((cap + 5) * sup.failure_threshold()):
        sup.handle_probe_result(state, alive=False)

    assert respawns["n"] == cap, (
        f"crash-looping child respawned {respawns['n']} times; the finite "
        f"window (cap={cap}) must stop respawns at exactly the cap"
    )


# ---------------------------------------------------------------------------
# VER — patch-bumped past 6.5.6 with GH #162 release notes.
# ---------------------------------------------------------------------------
def test_version_bumped_for_lead_respawn_grace():
    ver = importlib.import_module("prism_service.__version__")
    parts = tuple(int(x) for x in ver.PRISM_VERSION.split("."))
    assert parts >= (6, 5, 7), (
        f"PRISM_VERSION {ver.PRISM_VERSION} not bumped past 6.5.6 for the "
        f"lead-mode + post-respawn-grace change"
    )
    assert "162" in ver.PRISM_VERSION_NOTES, (
        "release notes do not mention GH #162"
    )
