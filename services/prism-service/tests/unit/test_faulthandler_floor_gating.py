"""Faulthandler all-thread-dump floor gating (reliability fix).

main._configure_logging arms a repeat=True faulthandler.dump_traceback_later
"floor" timer so a GIL-starved wedge still dumps all-thread stacks. The ONLY
thing that suppresses that free-running dump on a HEALTHY server is the
watchdog's per-cycle cancel-on-healthy. So when PRISM_WATCHDOG=off nothing
cancels it and a perfectly healthy, idle daemon dumps every interval forever
(the "Timeout (0:00:30)! …all-thread dump" spam that bloated prism.log to
>14MB and masqueraded as a pre-death signal during the daemon-death triage).

These pin _fault_floor_interval(): the floor is armed only when its partner
watchdog is enabled, defaults to 30s, clamps to a 5s minimum, and is disabled
by PRISM_FAULTHANDLER_REPEAT_S<=0.
"""

from __future__ import annotations

import importlib


def _main():
    return importlib.import_module("prism_service.main")


def test_floor_armed_by_default(monkeypatch):
    monkeypatch.delenv("PRISM_WATCHDOG", raising=False)
    monkeypatch.delenv("PRISM_FAULTHANDLER_REPEAT_S", raising=False)
    assert _main()._fault_floor_interval() == 30


def test_floor_disabled_when_watchdog_off(monkeypatch):
    # The regression: watchdog off => nothing cancels the repeat timer => spam.
    monkeypatch.delenv("PRISM_FAULTHANDLER_REPEAT_S", raising=False)
    for off in ("off", "0", "false", "no", "OFF"):
        monkeypatch.setenv("PRISM_WATCHDOG", off)
        assert _main()._fault_floor_interval() is None, off


def test_floor_armed_when_watchdog_on(monkeypatch):
    monkeypatch.delenv("PRISM_FAULTHANDLER_REPEAT_S", raising=False)
    for on in ("on", "1", "true", "yes"):
        monkeypatch.setenv("PRISM_WATCHDOG", on)
        assert _main()._fault_floor_interval() == 30, on


def test_repeat_zero_disables_even_with_watchdog_on(monkeypatch):
    monkeypatch.setenv("PRISM_WATCHDOG", "on")
    monkeypatch.setenv("PRISM_FAULTHANDLER_REPEAT_S", "0")
    assert _main()._fault_floor_interval() is None


def test_interval_clamped_to_minimum(monkeypatch):
    monkeypatch.setenv("PRISM_WATCHDOG", "on")
    monkeypatch.setenv("PRISM_FAULTHANDLER_REPEAT_S", "2")
    assert _main()._fault_floor_interval() == 5


def test_custom_interval_respected(monkeypatch):
    monkeypatch.setenv("PRISM_WATCHDOG", "on")
    monkeypatch.setenv("PRISM_FAULTHANDLER_REPEAT_S", "120")
    assert _main()._fault_floor_interval() == 120


def test_garbage_interval_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PRISM_WATCHDOG", "on")
    monkeypatch.setenv("PRISM_FAULTHANDLER_REPEAT_S", "not-a-number")
    assert _main()._fault_floor_interval() == 30
