"""A dispatched `claude -p` child must not outlive the process that spawned it.

THE INCIDENT (2026-09-11). Ten `claude -p` children were live on the dev host
with no parent. Seven of them ran inside fixture directories that had already
been deleted, the oldest 32 minutes old, every one carrying --max-budget-usd
and still spending. `subprocess.run` reaps a child on its OWN timeout, but it
never runs at all when the parent dies hard -- a pytest session under
`timeout 3000`, or a killed daemon -- so the child is reparented to init and
runs to its budget with nobody reading the result.

These tests pin the BEHAVIOUR (the child dies when the parent dies), not the
spelling of the command, plus the one command-shape fact another module
depends on: dispatch_guard's reaper finds children by a "claude -p" prefix.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from prism_service.inference import claude_cli


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


requires_setpriv = pytest.mark.skipif(
    not claude_cli._setpriv_path(),
    reason="host has no setpriv with --pdeathsig",
)


@requires_setpriv
def test_the_child_dies_when_its_parent_is_killed():
    """The real guarantee: kill the parent, the grandchild goes too.

    A parent python spawns a long `sleep` through the same wrapper the
    dispatcher uses, prints its pid, then we SIGKILL the parent -- the way a
    `timeout` or an OOM kill ends a test session, giving the parent no chance
    to clean up. Without PR_SET_PDEATHSIG the sleep survives as an orphan,
    which is exactly the incident above.
    """
    wrapped = claude_cli._with_parent_death_signal(["sleep", "60"])
    child_src = (
        "import subprocess,sys\n"
        f"p = subprocess.Popen({wrapped!r})\n"
        "print(p.pid, flush=True)\n"
        "p.wait()\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", child_src], stdout=subprocess.PIPE, text=True,
    )
    try:
        grandchild = int(parent.stdout.readline().strip())
        assert _alive(grandchild), "sleep should be running before we kill"

        parent.kill()          # SIGKILL: no cleanup path runs
        parent.wait(timeout=10)

        deadline = time.time() + 10
        while time.time() < deadline and _alive(grandchild):
            time.sleep(0.1)
        assert not _alive(grandchild), (
            f"pid {grandchild} outlived its parent -- this is the orphan"
        )
    finally:
        if parent.poll() is None:
            parent.kill()


@requires_setpriv
def test_the_wrapped_command_still_looks_like_claude_to_the_reaper():
    """dispatch_guard.sweep_reap matches children by a "claude -p" prefix.

    setpriv EXECS the program it wraps, so the running process keeps the
    argv of the wrapped command. If this ever became a wrapper that does NOT
    exec, /proc/<pid>/cmdline would read "setpriv ..." and the reaper would
    silently stop finding anything.
    """
    wrapped = claude_cli._with_parent_death_signal(["claude", "-p", "hello"])
    assert wrapped[-3:] == ["claude", "-p", "hello"]
    assert "--pdeathsig" in wrapped
    # The payload begins right after the "--" terminator.
    assert wrapped[wrapped.index("--") + 1] == "claude"


def test_a_host_without_setpriv_still_spawns(monkeypatch):
    """No util-linux must not mean no dispatch -- fall back to today's shape."""
    monkeypatch.setattr(claude_cli, "_setpriv_path", lambda: "")
    assert claude_cli._with_parent_death_signal(["claude", "-p", "x"]) == [
        "claude", "-p", "x",
    ]


def test_invoke_spawns_through_the_death_signal_wrapper(tmp_path, monkeypatch):
    """The wrapper is applied at the real call site, not just available."""
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(claude_cli, "_setpriv_path", lambda: "/usr/bin/setpriv")
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    claude_cli.invoke("hi", tmp_path, tmp_path, model="m", max_turns=1)

    assert seen["cmd"][0] == "/usr/bin/setpriv"
    assert seen["cmd"][1:4] == ["--pdeathsig", "TERM", "--"]
    assert seen["cmd"][4] == "claude"
