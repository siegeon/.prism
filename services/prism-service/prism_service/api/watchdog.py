"""Watchdog API (GH #155) — the deadlock-watchdog status surface.

Mirrors the maintenance heartbeat surface (api/consolidation.py
_maintenance_clock_row): a thin read-through to the module-level watchdog
status dict, plus the tunables the SPA's Diagnostics card renders.
"""

from fastapi import APIRouter

from prism_service.services import watchdog as wd

router = APIRouter()


@router.get("")
def status() -> dict:
    """Watchdog status for the SPA Diagnostics card. Carries at least
    last_probe_ok, consecutive_failures, dump_count (plus latency, last dump
    time, restarts, armed) alongside the live tunables (enabled/interval/
    timeout/kill) so the card can render the full health summary."""
    st = wd.watchdog_status()
    st.update({
        "enabled": wd.is_enabled(),
        "interval_s": wd.interval_s(),
        "timeout_s": wd.timeout_s(),
        "kill_enabled": wd._kill_enabled(),
    })
    return st
