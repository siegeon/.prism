"""Daemon self-heal (task 3576dd3f, v6.7.1) — a paying customer's instance
must recover from any crash. Three confirmed bugs are pinned here:

  AC-1 PIDFILE TRUTH — the SERVER (the process that binds the uvicorn ports)
       writes the pidfile with its OWN pid, so prism status/stop and the
       supervisor act on the real listener, never an intermediate launcher pid.
  AC-2 RESPAWN ON DEATH — when the watched server PROCESS is gone (not merely
       slow), the supervisor respawns a fresh server and rewrites the pidfile
       to the new live pid. Process-gone is the STRONG trigger (no waiting out
       N HTTP timeouts).
  AC-3 NO FALSE POSITIVE — an alive-but-slow probe inside the startup grace is
       NEVER force-killed; only a genuinely dead/unreachable-past-threshold
       server is killed+respawned.
  AC-4 the CREATE_BREAKAWAY_FROM_JOB fallback logs a loud WARNING (diagnosable,
       no longer a silent re-trap).
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import os
import sys

import pytest


# ---------------------------------------------------------------------------
# AC-1 — pidfile truth: the server writes its OWN pid.
# ---------------------------------------------------------------------------
def test_server_writes_own_pid_to_pidfile(tmp_path, monkeypatch):
    """main._own_pidfile_write() publishes the CURRENT process's pid — the one
    that binds the uvicorn sockets — so the pidfile names the real listener."""
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    main = importlib.import_module("prism_service.main")
    assert hasattr(main, "_own_pidfile_write"), (
        "main.py exposes no _own_pidfile_write() — the server cannot own the "
        "pidfile, so it keeps recording the launcher pid (pidfile lies)"
    )
    main._own_pidfile_write()
    from prism_service.data_dir import pid_file
    assert pid_file().read_text(encoding="utf-8").strip() == str(os.getpid()), (
        "pidfile does not name the server's own pid after _own_pidfile_write"
    )


def test_main_block_wires_own_pidfile_write():
    """The __main__ boot path must CALL _own_pidfile_write() — a defined-but-
    never-called helper is dead code that leaves the launcher pid in place."""
    main = importlib.import_module("prism_service.main")
    src = inspect.getsource(main)
    assert "_own_pidfile_write()" in src, (
        "main.__main__ never calls _own_pidfile_write() — the server does not "
        "publish its own pid (pidfile truth not wired)"
    )


# ---------------------------------------------------------------------------
# AC-2 — respawn when the watched server PROCESS is gone (strong trigger).
# ---------------------------------------------------------------------------
def test_handle_liveness_respawns_on_process_gone(monkeypatch, tmp_path):
    """A dead watched pid (process gone) triggers a respawn that rewrites the
    pidfile to the NEW live pid and rotates state.server_pid — WITHOUT waiting
    out N HTTP probe timeouts."""
    sup = importlib.import_module("prism_service.services.supervisor")
    assert hasattr(sup, "handle_liveness"), (
        "supervisor.py exposes no handle_liveness() — there is no process-gone "
        "respawn trigger (only the HTTP-probe path)"
    )
    pidfile = tmp_path / "prism.pid"
    pidfile.write_text("11111", encoding="utf-8")
    monkeypatch.setattr(sup, "_pidfile_path", lambda: pidfile, raising=False)
    monkeypatch.setattr(sup, "_spawn_server", lambda *a, **k: 22222, raising=False)
    monkeypatch.setattr(sup, "_in_startup_grace", lambda st: False, raising=False)

    state = sup.SupervisorState(server_pid=11111)
    fired = sup.handle_liveness(state, pid_alive=False)
    assert fired is True, "process-gone did not fire a respawn"
    assert state.server_pid == 22222, "watched pid did not rotate to the respawned pid"
    assert pidfile.read_text(encoding="utf-8").strip() == "22222", (
        "pidfile was not rewritten to the new live pid after respawn"
    )


def test_handle_liveness_noop_when_process_alive(monkeypatch):
    """A LIVE process is never respawned by the liveness trigger — a busy
    (alive-but-slow) daemon must be left to the graced HTTP-probe path."""
    sup = importlib.import_module("prism_service.services.supervisor")
    respawns = {"n": 0}
    monkeypatch.setattr(
        sup, "respawn_server",
        lambda *a, **k: respawns.__setitem__("n", respawns["n"] + 1) or 9,
        raising=False,
    )
    state = sup.SupervisorState(server_pid=4242)
    assert sup.handle_liveness(state, pid_alive=True) is False
    assert respawns["n"] == 0 and state.server_pid == 4242, (
        "a live server was respawned by the process-gone trigger"
    )


def test_supervise_once_respawns_dead_watched_pid(monkeypatch, tmp_path):
    """The per-cycle driver supervise_once() respawns a dead watched server on
    the STRONG (process-gone) trigger — the HTTP probe is not even consulted."""
    sup = importlib.import_module("prism_service.services.supervisor")
    assert hasattr(sup, "supervise_once"), (
        "supervisor.py exposes no supervise_once() per-cycle driver"
    )
    pidfile = tmp_path / "prism.pid"
    pidfile.write_text("700", encoding="utf-8")
    monkeypatch.setattr(sup, "_pidfile_path", lambda: pidfile, raising=False)
    monkeypatch.setattr(sup, "server_process_alive", lambda pid: False, raising=False)
    monkeypatch.setattr(sup, "_in_startup_grace", lambda st: False, raising=False)
    monkeypatch.setattr(sup, "probe_alive", lambda *a, **k: False)  # must be unused
    spawned = {"n": 0}
    monkeypatch.setattr(
        sup, "_spawn_server",
        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1) or 800,
        raising=False,
    )
    state = sup.SupervisorState(server_pid=700)
    sup.supervise_once(state, "8888")
    assert spawned["n"] == 1, "process-gone did not trigger a respawn in supervise_once"
    assert pidfile.read_text(encoding="utf-8").strip() == "800"
    assert state.server_pid == 800


# ---------------------------------------------------------------------------
# AC-3 — no false positive: alive-but-slow inside grace is never killed.
# ---------------------------------------------------------------------------
def test_alive_but_slow_within_grace_is_not_killed(monkeypatch, tmp_path):
    """A LIVE process whose HTTP probe is hung WHILE inside the startup grace
    must NOT be force-killed or respawned (the v6.6.1 false-positive class)."""
    sup = importlib.import_module("prism_service.services.supervisor")
    pidfile = tmp_path / "prism.pid"
    pidfile.write_text("700", encoding="utf-8")
    monkeypatch.setattr(sup, "_pidfile_path", lambda: pidfile, raising=False)
    monkeypatch.setattr(sup, "server_process_alive", lambda pid: True, raising=False)
    monkeypatch.setattr(sup, "_in_startup_grace", lambda st: True, raising=False)
    monkeypatch.setattr(sup, "probe_alive", lambda *a, **k: False)  # slow/hung HTTP
    killed = {"n": 0}
    spawned = {"n": 0}
    monkeypatch.setattr(sup, "force_kill", lambda *a, **k: killed.__setitem__("n", killed["n"] + 1))
    monkeypatch.setattr(
        sup, "_spawn_server",
        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1) or 9, raising=False,
    )
    state = sup.SupervisorState(server_pid=700)
    for _ in range(sup.failure_threshold() * 3):
        sup.supervise_once(state, "8888")
    assert killed["n"] == 0 and spawned["n"] == 0, (
        "an alive-but-slow server inside its startup grace was force-killed"
    )


def test_server_process_alive_distinguishes_dead_pid(monkeypatch):
    """server_process_alive() is a PROCESS-level check (does the pid exist),
    distinct from the HTTP probe. A clearly-dead/nonsense pid reads False; the
    current test process reads True."""
    sup = importlib.import_module("prism_service.services.supervisor")
    assert hasattr(sup, "server_process_alive"), (
        "supervisor.py exposes no server_process_alive() liveness check"
    )
    assert sup.server_process_alive(0) is False
    assert sup.server_process_alive(os.getpid()) is True


# ---------------------------------------------------------------------------
# AC-4 — the CREATE_BREAKAWAY_FROM_JOB fallback logs a loud WARNING.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not sys.platform.startswith("win"), reason="breakaway is Windows-only")
def test_cmd_start_breakaway_fallback_warns(tmp_path, monkeypatch, capsys):
    """When the launching job forbids CREATE_BREAKAWAY_FROM_JOB, cmd_start must
    WARN loudly (not silently re-trap) before falling back."""
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRISM_SUPERVISOR", "off")  # don't spawn the supervisor child
    cli = importlib.import_module("prism_service.cli.prism_cli")
    monkeypatch.setattr(cli, "_read_pid", lambda: 0)

    calls = {"n": 0}

    class _P:
        pid = 4242

    def _popen(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "breakaway forbidden")
        return _P()

    monkeypatch.setattr(cli.subprocess, "Popen", _popen)
    rc = cli.cmd_start(argparse.Namespace(daemon=True, ui_port=8899, mcp_port=8898))
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "BREAKAWAY" in err.upper(), (
        "breakaway fallback did not log a loud WARNING"
    )
