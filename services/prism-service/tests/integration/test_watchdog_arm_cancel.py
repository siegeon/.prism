"""Red scaffold (integration) — deadlock watchdog (GH #155).

The silent wedge raises no fatal signal, so faulthandler.enable() never
dumps. The watchdog closes that gap: each cycle it ARMS
faulthandler.dump_traceback_later(timeout, repeat=False) then runs a real
in-process HTTP self-probe. On a healthy probe it CANCELS the armed timer
and records latency; on a HANGING probe it does NOT cancel, so the C-level
timer fires and dumps every thread's stack into prism.log.

These tests pin the real behavior — arm-then-cancel on health, arm-then-
NO-cancel on hang — plus the lifespan wiring (the watchdog must actually be
STARTED beside the maintenance clock, not merely TestClient-importable).
"""

from __future__ import annotations

import inspect
import re

import pytest


def test_watchdog_module_shape():
    """watchdog.py exposes a start entrypoint + a status dict with the
    documented fields, mirroring the maintenance-clock worker shape."""
    wd = __import__("prism_service.services.watchdog", fromlist=["x"])
    assert hasattr(wd, "start_watchdog"), "no start_watchdog entrypoint"
    status = wd.watchdog_status()
    for field in ("last_probe_ok", "last_probe_latency_ms",
                  "consecutive_failures", "last_dump_at",
                  "dump_count", "restarts"):
        assert field in status, f"status dict missing {field!r}"


def test_healthy_probe_arms_then_cancels(monkeypatch):
    """One watchdog cycle against a HEALTHY prober arms the C timer and
    then cancels it, recording last_probe_ok=True + a latency."""
    wd = __import__("prism_service.services.watchdog", fromlist=["x"])

    calls = {"arm": 0, "cancel": 0}
    monkeypatch.setattr(
        wd.faulthandler, "dump_traceback_later",
        lambda *a, **k: calls.__setitem__("arm", calls["arm"] + 1),
    )
    monkeypatch.setattr(
        wd.faulthandler, "cancel_dump_traceback_later",
        lambda *a, **k: calls.__setitem__("cancel", calls["cancel"] + 1),
    )
    # Healthy prober: returns quickly without raising.
    monkeypatch.setattr(wd, "_self_probe", lambda timeout_s: 3.0)

    wd._run_cycle(timeout_s=20)

    assert calls["arm"] == 1, "watchdog did not arm faulthandler"
    assert calls["cancel"] == 1, "watchdog did not cancel after a healthy probe"
    st = wd.watchdog_status()
    assert st["last_probe_ok"] is True
    assert st["last_probe_latency_ms"] is not None


def test_hanging_probe_arms_and_does_not_cancel(monkeypatch):
    """One cycle against a HANGING prober arms the timer and does NOT
    cancel it — so the C-level timer is left to fire and dump all stacks.
    consecutive_failures increments."""
    wd = __import__("prism_service.services.watchdog", fromlist=["x"])

    calls = {"arm": 0, "cancel": 0}
    monkeypatch.setattr(
        wd.faulthandler, "dump_traceback_later",
        lambda *a, **k: calls.__setitem__("arm", calls["arm"] + 1),
    )
    monkeypatch.setattr(
        wd.faulthandler, "cancel_dump_traceback_later",
        lambda *a, **k: calls.__setitem__("cancel", calls["cancel"] + 1),
    )

    def _hang(timeout_s):
        raise TimeoutError("probe hung")

    monkeypatch.setattr(wd, "_self_probe", _hang)

    before = wd.watchdog_status()["consecutive_failures"]
    wd._run_cycle(timeout_s=20)

    assert calls["arm"] == 1, "watchdog did not arm before the hanging probe"
    assert calls["cancel"] == 0, (
        "watchdog cancelled the timer on a HANGING probe — the C-level "
        "dump would never fire, defeating the whole point of #155"
    )
    st = wd.watchdog_status()
    assert st["last_probe_ok"] is False
    assert st["consecutive_failures"] == before + 1


def test_kill_is_opt_in(monkeypatch):
    """Self-heal os._exit(1) only fires when PRISM_WATCHDOG_KILL=1 and the
    failure streak exceeds threshold; default OFF."""
    wd = __import__("prism_service.services.watchdog", fromlist=["x"])
    monkeypatch.delenv("PRISM_WATCHDOG_KILL", raising=False)
    assert wd._kill_enabled() is False, "kill must default OFF"
    monkeypatch.setenv("PRISM_WATCHDOG_KILL", "1")
    assert wd._kill_enabled() is True, "PRISM_WATCHDOG_KILL=1 must enable kill"


def test_main_lifespan_wires_watchdog():
    """main.py's startup must import start_watchdog and spawn it, beside
    the maintenance clock — not just leave it importable for a TestClient."""
    import prism_service.main as main_mod

    src = inspect.getsource(main_mod)
    assert "start_watchdog" in src, (
        "main.py does not reference start_watchdog — the watchdog is not "
        "wired into the daemon lifecycle (dead code)"
    )
    assert re.search(r"watchdog\s+import\s+start_watchdog", src), (
        "main.py does not import start_watchdog from "
        "prism_service.services.watchdog"
    )
