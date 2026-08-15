"""Stale-lock recovery on lifespan boot (v5.1.7).

Watchtower swaps the container while data/.mcp_started survives in the
mounted volume. The old behavior treated any pre-existing lock as
"another process is already running" and skipped starting every
background thread (governance, drift, quality, the v5.1.6
understand_drainer). The new behavior reclaims a stale lock and starts
the threads anyway.

Uses tmp_path so we never touch the real /data/.mcp_started.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_lock(tmp_path, monkeypatch):
    import prism_service.main as main_mod
    monkeypatch.setattr(main_mod, "_LOCK_FILE", tmp_path / ".mcp_started")
    # The drive-activity observer (task dd1e8871) keeps a module-level
    # singleton so a live process never double-starts its thread. Mocked
    # Threads read as alive forever, so without a reset the SECOND lifespan
    # run in this process would no-op the start and undercount by one.
    # A real stale-lock recovery is a fresh process where _thread is None.
    import prism_service.services.drive_activity_observer as dao_mod
    monkeypatch.setattr(dao_mod, "_thread", None)
    return tmp_path / ".mcp_started"


def _run_lifespan(app=None) -> None:
    """Enter and exit the lifespan context once, synchronously."""
    import prism_service.main as main_mod
    cm = main_mod.lifespan(app or object())

    async def runner():
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)

    asyncio.run(runner())


def test_lifespan_starts_threads_when_no_lock(isolated_lock):
    assert not isolated_lock.exists()
    with patch("prism_service.main.threading.Thread") as mock_t, \
            patch("prism_service.main._install_stackdump_handler"):
        _run_lifespan()

    # 10 daemon threads as of v6.5.0 (GH #155 added the deadlock watchdog).
    # History: epic 4fd1e6b4 Phase 3 RETIRED the Reflection + Memory Summary
    # timers onto the event-pool bus; Phase 4 FOLDED governance + quality +
    # adaptive-policy into ONE maintenance_clock; 13 -> 11 -> 9 -> 10:
    #   5 explicit in main.py — mcp + drift + understand_drainer +
    #     trash_sweeper + transcript_importer
    #   1 from start_auto_updater() (always starts)
    #   1 from start_graph_enrich_worker() (defaults ON, v6.2.0+)
    #   1 from start_event_pool() (defaults ON, Phase 1 — runs the REAL
    #     migrated reflection/dedup/credit handlers on the bus)
    #   1 from the maintenance_clock (Phase 4 — single heartbeat running
    #     governance TTL/decay/dup, verify-staleness, forget, adaptive retune,
    #     quality-vs-git, each behind its own cadence gate)
    #   1 from start_watchdog (GH #155 — deadlock watchdog; defaults ON,
    #     PRISM_WATCHDOG=off to disable)
    # RETIRED (no longer spawned): start_reflection_worker(),
    # start_memory_summary_worker() (Phase 3); start_governance_timer,
    # start_quality_timer, start_adaptive_policy_worker (Phase 4 — folded into
    # maintenance_clock). start_memory_ops_workers() is OFF by default (0 here).
    # patch("prism_service.main.threading.Thread") mutates the global
    # threading module, so threads started inside the indirectly-imported
    # services are also intercepted.
    started = [c for c in mock_t.return_value.start.mock_calls]
    # Still 10 as of v7.0.32: the green-gate machine adjudicator sweep
    # (task 1d3322a6, services/gate_adjudicator.py) ships OFF by default —
    # its thread only starts when PRISM_GATE_ADJUDICATOR_INTERVAL opts in.
    # 11 as of v7.10.49: the drive-activity observer (task dd1e8871,
    # services/drive_activity_observer.py) starts a thread that derives
    # heartbeats from observed activity so long steps stay visibly alive.
    assert len(started) == 11


def test_lifespan_reclaims_stale_lock_and_starts_threads(isolated_lock, capsys):
    # Simulate a previous container's lock left behind.
    isolated_lock.write_text("999999", encoding="utf-8")
    assert isolated_lock.exists()

    with patch("prism_service.main.threading.Thread") as mock_t, \
            patch("prism_service.main._install_stackdump_handler"):
        _run_lifespan()

    started = [c for c in mock_t.return_value.start.mock_calls]
    # Same 11 threads — see test_lifespan_starts_threads_when_no_lock.
    assert len(started) == 11  # threads started despite the stale lock

    err = capsys.readouterr().err
    assert "stale lock detected" in err
    assert "999999" in err  # the prior tid is reported


def test_lifespan_unlinks_lock_on_exit(isolated_lock):
    with patch("prism_service.main.threading.Thread"), \
            patch("prism_service.main._install_stackdump_handler"):
        _run_lifespan()
    assert not isolated_lock.exists()
