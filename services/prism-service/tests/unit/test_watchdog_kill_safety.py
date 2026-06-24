"""Watchdog kill-safety (GH #155/#162 follow-up, v6.6.1).

The in-process deadlock watchdog must NEVER os._exit a busy-but-HEALTHY daemon.
A real all-threads deadlock is permanent; a busy spell (model-load, indexing, a
maintenance pass) blocks the single event loop for tens of seconds and makes the
HTTP self-probe time out — that is not a wedge. After #162 flipped the kill
default ON, that false-positive killed the daemon on a ~90s loop (fatal in dev,
which has no out-of-process supervisor to respawn).

These pin the fix: (1) a hang inside the startup GRACE never kills and disarms
the dump; (2) past grace, a single transient hang does NOT kill (the wedge has
not persisted KILL_AFTER_S); (3) a CONTINUOUS wedge older than KILL_AFTER_S with
kill enabled DOES os._exit; (4) kill disabled never exits; (5) a healthy probe
clears the failure streak.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def wd(monkeypatch):
    w = __import__("prism_service.services.watchdog", fromlist=["x"])
    saved = (w._started_at, w._first_fail_at)
    # Neutralise the real faulthandler timer + start from a clean cycle state.
    monkeypatch.setattr(w.faulthandler, "dump_traceback_later", lambda *a, **k: None)
    monkeypatch.setattr(w.faulthandler, "cancel_dump_traceback_later", lambda *a, **k: None)
    w._first_fail_at = None
    with w._status_lock:
        w._status.update(consecutive_failures=0, dump_count=0, restarts=0,
                         last_probe_ok=None, last_dump_at=None, armed=False)
    yield w
    # Restore module globals so this test can't make the arm/cancel tests see a
    # "recent" _started_at (which would push their hang into the grace branch).
    w._started_at, w._first_fail_at = saved


def _hang(_timeout_s):
    raise TimeoutError("probe hung")


def _count_exits(wd, monkeypatch):
    seen = {"n": 0}
    monkeypatch.setattr(wd.os, "_exit", lambda code: seen.__setitem__("n", seen["n"] + 1))
    return seen


def test_hang_in_startup_grace_never_kills(wd, monkeypatch):
    seen = _count_exits(wd, monkeypatch)
    monkeypatch.setattr(wd, "_self_probe", _hang)
    monkeypatch.setattr(wd, "_kill_enabled", lambda: True)
    monkeypatch.setenv("PRISM_WATCHDOG_GRACE_S", "180")
    wd._started_at = wd.time.time()          # just booted -> inside grace
    for _ in range(10):                       # many failures, still in grace
        wd._run_cycle(timeout_s=20)
    assert seen["n"] == 0, "watchdog os._exit-ed during the startup grace window"


def test_single_transient_hang_past_grace_does_not_kill(wd, monkeypatch):
    seen = _count_exits(wd, monkeypatch)
    monkeypatch.setattr(wd, "_self_probe", _hang)
    monkeypatch.setattr(wd, "_kill_enabled", lambda: True)
    monkeypatch.setenv("PRISM_WATCHDOG_GRACE_S", "0")
    monkeypatch.setenv("PRISM_WATCHDOG_KILL_AFTER_S", "300")
    wd._started_at = wd.time.time() - 10_000  # long past grace
    wd._run_cycle(timeout_s=20)               # first failure: wedge_s ~ 0
    assert seen["n"] == 0, "watchdog killed on a single transient (busy-spell) hang"


def test_sustained_continuous_wedge_kills(wd, monkeypatch):
    seen = _count_exits(wd, monkeypatch)
    monkeypatch.setattr(wd, "_self_probe", _hang)
    monkeypatch.setattr(wd, "_kill_enabled", lambda: True)
    monkeypatch.setenv("PRISM_WATCHDOG_GRACE_S", "0")
    monkeypatch.setenv("PRISM_WATCHDOG_KILL_AFTER_S", "300")
    wd._started_at = wd.time.time() - 10_000
    wd._first_fail_at = wd.time.time() - 600  # wedge has persisted 10 min
    wd._run_cycle(timeout_s=20)
    assert seen["n"] == 1, "a continuous wedge older than KILL_AFTER_S must os._exit"


def test_kill_disabled_never_exits(wd, monkeypatch):
    seen = _count_exits(wd, monkeypatch)
    monkeypatch.setattr(wd, "_self_probe", _hang)
    monkeypatch.setattr(wd, "_kill_enabled", lambda: False)
    monkeypatch.setenv("PRISM_WATCHDOG_GRACE_S", "0")
    monkeypatch.setenv("PRISM_WATCHDOG_KILL_AFTER_S", "300")
    wd._started_at = wd.time.time() - 10_000
    wd._first_fail_at = wd.time.time() - 600
    wd._run_cycle(timeout_s=20)
    assert seen["n"] == 0, "kill disabled (PRISM_WATCHDOG_KILL=0) must never os._exit"


def test_healthy_probe_clears_failure_streak(wd, monkeypatch):
    monkeypatch.setattr(wd, "_self_probe", lambda _timeout_s: 2.0)
    wd._first_fail_at = wd.time.time() - 600
    wd._run_cycle(timeout_s=20)
    assert wd._first_fail_at is None, "healthy probe did not reset the failure streak"
    assert wd.watchdog_status()["consecutive_failures"] == 0
