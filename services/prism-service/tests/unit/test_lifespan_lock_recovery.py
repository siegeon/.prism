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
import re
from pathlib import Path
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


def _prism_threads(mock_t):
    """Constructor calls whose target is prism_service code.

    CI runners construct one extra thread during startup that a dev
    machine does not (a dependency difference, not a worker change), so a
    raw total is not hermetic — 14 there, 13 here, same code (task
    a2bc8c88). Pin OUR workers by the target's defining module instead:
    a new prism worker still moves this count, a dependency's internal
    thread never does.
    """
    calls = []
    for c in mock_t.call_args_list:
        tgt = c.kwargs.get("target")
        if tgt is None and c.args:
            tgt = c.args[0]
        tgt = getattr(tgt, "func", tgt)  # unwrap functools.partial
        mod = getattr(tgt, "__module__", "") or ""
        if mod.startswith("prism_service"):
            calls.append(c)
    return calls


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
    started = _prism_threads(mock_t)
    # Still 10 as of v7.0.32: the green-gate machine adjudicator sweep
    # (task 1d3322a6, services/gate_adjudicator.py) ships OFF by default —
    # its thread only starts when PRISM_GATE_ADJUDICATOR_INTERVAL opts in.
    # 11 as of v7.10.49: the drive-activity observer (task dd1e8871,
    # services/drive_activity_observer.py) starts a thread that derives
    # heartbeats from observed activity so long steps stay visibly alive.
    # 12 as of the gamify walking skeleton ("PRISM shows its work"):
    # start_work_ticker (services/work_stream.py) polls active managed
    # tasks' linked-session transcripts every ~1.5s and publishes
    # tokens.turn onto the bus for /sse/work + the /live graph — live
    # token burn isn't event-shaped, so nothing else pushes it.
    # 13 as of task b0138f17 ("a cold PRISM answers without calling the
    # internet"): warm_embedder (engines/brain_engine.py) preloads the
    # embedding model at boot, offline-first, so the first request after
    # a restart never pays the model load or a huggingface round trip.
    # RED [task:a2bc8c88]: pins a literal total; env-gated environments start 18 prism threads (task_runner, gate_adjudicator, ship_worker, language_alignment_worker, resume_actuator join the 13)
    assert len(started) == 13


def test_lifespan_reclaims_stale_lock_and_starts_threads(isolated_lock, capsys):
    # Simulate a previous container's lock left behind.
    isolated_lock.write_text("999999", encoding="utf-8")
    assert isolated_lock.exists()

    with patch("prism_service.main.threading.Thread") as mock_t, \
            patch("prism_service.main._install_stackdump_handler"):
        _run_lifespan()

    started = _prism_threads(mock_t)
    # Same 13 threads — see test_lifespan_starts_threads_when_no_lock.
    # RED [task:a2bc8c88]: pins a literal total; env-gated environments start 18 prism threads (task_runner, gate_adjudicator, ship_worker, language_alignment_worker, resume_actuator join the 13)
    assert len(started) == 13  # threads started despite the stale lock

    err = capsys.readouterr().err
    assert "stale lock detected" in err
    assert "999999" in err  # the prior tid is reported


def test_lifespan_unlinks_lock_on_exit(isolated_lock):
    with patch("prism_service.main.threading.Thread"), \
            patch("prism_service.main._install_stackdump_handler"):
        _run_lifespan()
    assert not isolated_lock.exists()
