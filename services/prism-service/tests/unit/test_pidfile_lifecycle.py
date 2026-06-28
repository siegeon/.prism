"""Issue #66 — pidfile lifecycle: atomic write, stale-cleanup on start,
and the HTTP-identity cross-check that stops status/stop from trusting a
recycled PID.

These exercise prism_service.cli.prism_cli with PRISM_DATA_DIR pointed at
a tmp dir so no real daemon is touched.
"""

from __future__ import annotations

import argparse
import types

import pytest

from prism_service.cli import prism_cli as cli


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    return tmp_path


def test_write_pid_is_atomic_and_leaves_no_tmp(data_dir):
    cli._write_pid(4321)
    assert cli._pid_file().read_text(encoding="utf-8").strip() == "4321"
    # The tmp file used for the atomic os.replace must not linger.
    assert not cli._pid_file().with_suffix(".pid.tmp").exists()


def test_read_pid_absent_is_zero(data_dir):
    assert cli._read_pid() == 0


def test_is_alive_rejects_nonpositive(data_dir):
    assert cli._is_alive(0) is False
    assert cli._is_alive(-1) is False


def test_cmd_start_clears_confirmed_stale_pidfile(data_dir, monkeypatch, capsys):
    # An old, dead pid is sitting in the pidfile.
    cli._write_pid(999999)
    monkeypatch.setattr(cli, "_is_alive", lambda pid: False)
    # GH #181 port-level guard: neutralize it so this stale-pidfile test still
    # reaches the spawn path (no daemon answering, no port held).
    monkeypatch.setattr(cli, "_daemon_http_alive", lambda port: False)
    monkeypatch.setattr(cli, "_port_owner_pid", lambda port: None)

    launched = {}

    class _FakeProc:
        pid = 12345

    def _fake_popen(*args, **kwargs):
        launched["stdout"] = kwargs.get("stdout")
        return _FakeProc()

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)
    ns = argparse.Namespace(daemon=True, ui_port=None, mcp_port=None)
    rc = cli.cmd_start(ns)

    assert rc == 0
    err = capsys.readouterr().err
    assert "removing stale pidfile" in err
    assert cli._read_pid() == 12345
    # The child's stdout is redirected to prism.log (append) so the
    # daemon's output is captured on the daemon path (#66).
    out_handle = launched["stdout"]
    assert out_handle is not None
    assert out_handle.name.endswith("prism.log")


def test_cmd_status_reports_stale_when_pid_alive_but_unresponsive(
    data_dir, monkeypatch, capsys,
):
    cli._write_pid(555)
    monkeypatch.setattr(cli, "_is_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_daemon_http_alive", lambda port: False)
    monkeypatch.setattr(cli, "_port_owner_pid", lambda port: None)  # #181: no split-brain
    cli.cmd_status(argparse.Namespace())
    out = capsys.readouterr().out
    assert "stale pidfile" in out
    assert "running (pid" not in out


def test_cmd_status_reports_running_when_responding(data_dir, monkeypatch, capsys):
    cli._write_pid(555)
    monkeypatch.setattr(cli, "_is_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_daemon_http_alive", lambda port: True)
    monkeypatch.setattr(cli, "_port_owner_pid", lambda port: None)  # #181: no split-brain
    cli.cmd_status(argparse.Namespace())
    out = capsys.readouterr().out
    assert "running (pid 555)" in out


def test_cmd_stop_refuses_to_kill_recycled_pid(data_dir, monkeypatch, capsys):
    cli._write_pid(777)
    monkeypatch.setattr(cli, "_is_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_daemon_http_alive", lambda port: False)

    killed = {"called": False}
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: killed.__setitem__("called", True))
    monkeypatch.setattr(cli.os, "kill",
                        lambda *a, **k: killed.__setitem__("called", True))

    rc = cli.cmd_stop(argparse.Namespace())
    assert rc == 0
    assert killed["called"] is False           # innocent PID not signalled
    assert not cli._pid_file().exists()         # but the stale pidfile is cleared
    assert "refusing to kill" in capsys.readouterr().err


def test_daemon_http_alive_false_on_dead_port(data_dir):
    # Nothing is listening on this port; probe must fail fast, not raise.
    assert cli._daemon_http_alive(59999) is False


# ── GH #181: port-level start guard, status self-heal, stop orphan sweep ──────
def test_cmd_start_refuses_when_ui_already_answering(data_dir, monkeypatch, capsys):
    # No pidfile at all (mid-rotation/absent), but a daemon IS answering on the
    # UI port — cmd_start must refuse to stack a second family (AC-2).
    monkeypatch.setattr(cli, "_daemon_http_alive", lambda port: True)
    monkeypatch.setattr(cli, "_port_owner_pid", lambda port: None)
    spawned = {"called": False}
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("called", True))
    rc = cli.cmd_start(argparse.Namespace(daemon=True, ui_port=None, mcp_port=None))
    assert rc == 1
    assert spawned["called"] is False
    assert "already running" in capsys.readouterr().err


def test_cmd_start_refuses_when_mcp_port_held(data_dir, monkeypatch, capsys):
    # UI not answering, but the MCP port is held by another pid (an in-flight
    # restart / orphan) — still refuse (AC-2).
    monkeypatch.setattr(cli, "_daemon_http_alive", lambda port: False)
    monkeypatch.setattr(cli, "_port_owner_pid", lambda port: 49019)
    spawned = {"called": False}
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("called", True))
    rc = cli.cmd_start(argparse.Namespace(daemon=True, ui_port=None, mcp_port=None))
    assert rc == 1
    assert spawned["called"] is False
    assert "already held by pid 49019" in capsys.readouterr().err


def test_cmd_status_reports_unhealthy_and_self_heals_split_brain(data_dir, monkeypatch, capsys):
    # Two pids each own one port -> UNHEALTHY + reap the orphan, exit non-zero (AC-3).
    cli._write_pid(26138)
    monkeypatch.setattr(cli, "_is_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_daemon_http_alive", lambda port: True)
    owners = {"7778": 26138, "7777": 49019}
    monkeypatch.setattr(cli, "_port_owner_pid", lambda port: owners.get(str(port)))
    killed = []
    from prism_service.services import split_brain
    monkeypatch.setattr(split_brain, "kill_pid_escalating",
                        lambda pid, *a, **k: killed.append(pid))
    rc = cli.cmd_status(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 1
    assert "UNHEALTHY" in out
    assert killed == [49019]              # orphan reaped, keeper (UI pid) spared


def test_cmd_stop_reaps_split_brain_orphan(data_dir, monkeypatch, capsys):
    cli._write_pid(26138)
    monkeypatch.setattr(cli, "_is_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_daemon_http_alive", lambda port: True)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)
    owners = {"7778": 26138, "7777": 49019}
    monkeypatch.setattr(cli, "_port_owner_pid", lambda port: owners.get(str(port)))
    killed = []
    from prism_service.services import split_brain
    monkeypatch.setattr(split_brain, "kill_pid_escalating",
                        lambda pid, *a, **k: killed.append(pid))
    rc = cli.cmd_stop(argparse.Namespace())
    assert rc == 0
    assert killed == [49019]             # the SIGTERM-ignoring orphan is reaped
    assert "reaped split-brain orphan pid 49019" in capsys.readouterr().err
