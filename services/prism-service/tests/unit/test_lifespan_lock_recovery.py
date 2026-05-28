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

    # 10 daemon threads as of v6.2.0:
    #   7 explicit in main.py — mcp + governance + drift + quality +
    #     understand_drainer + trash_sweeper + transcript_importer
    #   1 from start_auto_updater() (always starts)
    #   1 from start_memory_summary_worker() (defaults ON, v6.1.1+)
    #   1 from start_graph_enrich_worker() (defaults ON, v6.2.0+)
    # reflection_worker is OFF by default (PRISM_REFLECTION_WORKER=on
    # to opt in) so it does NOT add to the count here.
    # patch("prism_service.main.threading.Thread") mutates the global
    # threading module, so threads started inside the indirectly-imported
    # services are also intercepted.
    started = [c for c in mock_t.return_value.start.mock_calls]
    assert len(started) == 10


def test_lifespan_reclaims_stale_lock_and_starts_threads(isolated_lock, capsys):
    # Simulate a previous container's lock left behind.
    isolated_lock.write_text("999999", encoding="utf-8")
    assert isolated_lock.exists()

    with patch("prism_service.main.threading.Thread") as mock_t, \
            patch("prism_service.main._install_stackdump_handler"):
        _run_lifespan()

    started = [c for c in mock_t.return_value.start.mock_calls]
    # Same 10 threads — see test_lifespan_starts_threads_when_no_lock.
    assert len(started) == 10  # threads started despite the stale lock

    err = capsys.readouterr().err
    assert "stale lock detected" in err
    assert "999999" in err  # the prior tid is reported


def test_lifespan_unlinks_lock_on_exit(isolated_lock):
    with patch("prism_service.main.threading.Thread"), \
            patch("prism_service.main._install_stackdump_handler"):
        _run_lifespan()
    assert not isolated_lock.exists()
