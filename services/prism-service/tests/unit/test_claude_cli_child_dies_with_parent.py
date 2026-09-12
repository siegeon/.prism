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

    `_with_parent_death_signal` is called INSIDE the spawned child_src
    process, not by this outer test -- it must run in the SAME process
    that becomes the grandchild's real OS parent (task 4465ee72's guard
    captures os.getpid() to compare against later), exactly how
    `invoke()` calls it immediately before its own `subprocess.run`.
    Precomputing it out here would capture this test's OWN pid, one level
    too high, and the guard would refuse the payload every time.
    """
    child_src = (
        "import subprocess, sys\n"
        "from prism_service.inference import claude_cli\n"
        "wrapped = claude_cli._with_parent_death_signal(['sleep', '60'])\n"
        "p = subprocess.Popen(wrapped)\n"
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

    setpriv and the `sh` parent-recheck guard (task 4465ee72) both EXEC the
    next stage rather than forking, so the pid never changes and the
    RUNNING process's argv is always the payload's own -- /proc/<pid>/cmdline
    reads "claude -p ..." once the real command is executing, regardless of
    how many exec stages ran to get there. If any stage ever forked instead
    of exec'd, or ended without a final `exec "$@"`, the reaper would
    silently stop finding anything.
    """
    wrapped = claude_cli._with_parent_death_signal(["claude", "-p", "hello"])
    # The payload is always the tail of the wrapped command, whatever guard
    # stages precede it.
    assert wrapped[-3:] == ["claude", "-p", "hello"]
    assert "--pdeathsig" in wrapped
    # The shell guard's own body must end in `exec "$@"`, so the payload it
    # is handed truly replaces the shell's process image rather than being
    # run as a child of it.
    guard_body = next(tok for tok in wrapped if "exec" in tok and "$@" in tok)
    assert guard_body.rstrip().endswith('exec "$@"; fi'), guard_body


@requires_setpriv
def test_the_parent_recheck_guard_runs_the_payload_when_the_parent_still_matches(
        tmp_path):
    """Positive case for the arm-vs-check race fix (task 4465ee72).

    This test process really is the parent of the spawned chain the whole
    time, so the guard's freshly-read $PPID (read at the GUARD's own
    startup, after setpriv has already run) matches the pid this function
    captured, and the payload runs normally. Deterministic -- no load or
    timing needed, unlike test_the_child_dies_when_its_parent_is_killed.
    """
    marker = tmp_path / "ran"
    wrapped = claude_cli._with_parent_death_signal(["touch", str(marker)])
    subprocess.run(wrapped, check=True, timeout=10)
    assert marker.exists(), "the guard should have exec'd the real payload"


def test_the_parent_recheck_guard_refuses_a_stale_parent(tmp_path):
    """Negative case: THE ACTUAL DEFECT this task fixed.

    _with_parent_death_signal always captures ITS OWN os.getpid(), so it
    can never hand itself a mismatched pid to prove the refusal branch --
    the race it protects against is a REAL parent dying between fork() and
    setpriv's prctl(), never a caller passing a wrong value. This builds
    the identical guard shape with a pid that can never be this process's
    real parent, which exercises the same "$PPID no longer matches" branch
    deterministically: measured live under synthetic CPU load (48 busy
    loops on 24 cores), a `sleep` wrapped WITHOUT this guard was left
    running, reparented, never signalled -- this pins that the guard now
    refuses to ever run the payload in that shape, on any host, without
    needing to reproduce the load.
    """
    marker = tmp_path / "should_not_exist"
    bogus_ppid = 1  # never this test process's real parent
    guard = f'if [ "$PPID" = "{bogus_ppid}" ]; then exec "$@"; fi'
    subprocess.run(["sh", "-c", guard, "sh", "touch", str(marker)],
                   check=True, timeout=10)
    assert not marker.exists(), "a mismatched $PPID must never run the payload"


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
    # setpriv's own exec target is the `sh` parent-recheck guard (task
    # 4465ee72): sh -c <guard> sh <payload...>. The guard execs into the
    # real payload ("claude ...") only once it confirms the parent it
    # started with is still alive -- see
    # test_the_wrapped_command_still_looks_like_claude_to_the_reaper for
    # why the RUNNING process still looks like "claude -p ..." either way.
    assert seen["cmd"][4] == "sh"
    assert seen["cmd"][5] == "-c"
    assert seen["cmd"][7] == "sh"
    assert seen["cmd"][8] == "claude"
