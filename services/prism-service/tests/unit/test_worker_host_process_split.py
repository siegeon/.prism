"""Pins the worker-host process split (task: worker-host process split,
owner 2026-09-13): the nine standing workers ran as threads INSIDE the
FastAPI process, so any GIL-holding or git-blocking pass delayed every
HTTP request being served at the same moment (measured live: 10-20s routes
whenever a worker pass ran, 0.2s otherwise).

PRISM_WORKERS_PROCESS=1 must make the API process start ZERO of these
worker threads and instead spawn one child process
(`worker_host.main`) that starts them; PRISM_WORKERS_PROCESS=0 (or unset,
outside dev mode) must leave today's in-process-thread behavior
unchanged -- see test_lifespan_lock_recovery.py's CORE_WORKERS for the
existing contract this must not break.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def isolated_lock(tmp_path, monkeypatch):
    import prism_service.main as main_mod
    monkeypatch.setattr(main_mod, "_LOCK_FILE", tmp_path / ".mcp_started")
    monkeypatch.setattr(main_mod, "_WORKER_HOST_PROC", None)
    import prism_service.services.drive_activity_observer as dao_mod
    monkeypatch.setattr(dao_mod, "_thread", None)
    return tmp_path / ".mcp_started"


def _run_lifespan() -> None:
    import prism_service.main as main_mod
    cm = main_mod.lifespan(object())

    async def runner():
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)

    asyncio.run(runner())


def _prism_thread_names(mock_t) -> set:
    names = set()
    for c in mock_t.call_args_list:
        tgt = c.kwargs.get("target")
        if tgt is None and c.args:
            tgt = c.args[0]
        tgt = getattr(tgt, "func", tgt)
        mod = getattr(tgt, "__module__", "") or ""
        if mod.startswith("prism_service"):
            names.add(f"{mod}.{tgt.__qualname__}")
    return names


# ---------------------------------------------------------------------------
# _workers_process_enabled(): pure env-var semantics.
# ---------------------------------------------------------------------------

def test_workers_process_enabled_explicit_on(monkeypatch):
    import prism_service.main as main_mod
    monkeypatch.setenv("PRISM_WORKERS_PROCESS", "1")
    monkeypatch.delenv("PRISM_DEV_MODE", raising=False)
    assert main_mod._workers_process_enabled() is True


def test_workers_process_enabled_explicit_off_overrides_dev_mode(monkeypatch):
    import prism_service.main as main_mod
    monkeypatch.setenv("PRISM_WORKERS_PROCESS", "0")
    monkeypatch.setenv("PRISM_DEV_MODE", "1")
    assert main_mod._workers_process_enabled() is False


def test_workers_process_enabled_defaults_on_regardless_of_dev_mode(monkeypatch):
    # Owner speed-mode directive (2026-09-13): ship the fix live without a
    # separate opt-in -- unset PRISM_WORKERS_PROCESS means ON, whether or
    # not PRISM_DEV_MODE is set. An operator who needs the old
    # single-process shape sets PRISM_WORKERS_PROCESS=0 explicitly (see
    # test_workers_process_enabled_explicit_off_overrides_dev_mode above).
    import prism_service.main as main_mod
    monkeypatch.delenv("PRISM_WORKERS_PROCESS", raising=False)
    monkeypatch.delenv("PRISM_DEV_MODE", raising=False)
    assert main_mod._workers_process_enabled() is True
    monkeypatch.setenv("PRISM_DEV_MODE", "1")
    assert main_mod._workers_process_enabled() is True


# ---------------------------------------------------------------------------
# Lifespan wiring: PRISM_WORKERS_PROCESS=1 -> zero in-process worker
# threads, one spawned worker-host process.
# ---------------------------------------------------------------------------

_DIRECTLY_CALLED_WORKERS = [
    # (import path, attr name)
    ("prism_service.services.gate_adjudicator", "start_gate_adjudicator"),
    ("prism_service.services.task_runner", "start_task_runner"),
    ("prism_service.services.task_runner", "release_stale_seat_leases"),
    ("prism_service.services.resume_actuator", "start_resume_actuator"),
    ("prism_service.services.deploy_worker", "start_deploy_worker"),
    ("prism_service.services.dispatch_guard", "start_dispatch_reaper"),
    ("prism_service.services.language_alignment_worker",
     "start_language_alignment_worker"),
    ("prism_service.services.maintenance_clock", "start_maintenance_clock"),
    ("prism_service.services.ship_worker", "start_ship_worker"),
]


def _patch_directly_called_workers(monkeypatch) -> dict:
    import importlib
    mocks = {}
    for path, attr in _DIRECTLY_CALLED_WORKERS:
        mod = importlib.import_module(path)
        m = MagicMock()
        monkeypatch.setattr(mod, attr, m)
        mocks[f"{path}.{attr}"] = m
    return mocks


def test_workers_process_enabled_starts_zero_in_process_worker_threads(
        isolated_lock, monkeypatch):
    import prism_service.main as main_mod
    monkeypatch.setenv("PRISM_WORKERS_PROCESS", "1")
    mocks = _patch_directly_called_workers(monkeypatch)

    spawn_calls = []

    def _fake_spawn():
        spawn_calls.append(1)
        proc = MagicMock()
        proc.pid = 999999
        proc.is_alive.return_value = True
        return proc

    monkeypatch.setattr(main_mod, "_spawn_worker_host", _fake_spawn)

    from unittest.mock import patch
    with patch("prism_service.main.threading.Thread") as mock_t, \
            patch("prism_service.main._install_stackdump_handler"):
        _run_lifespan()

    # The child process was spawned exactly once...
    assert len(spawn_calls) == 1
    # ...drift (the one thread-wrapped migrated worker) was never threaded
    # directly in this process...
    assert "prism_service.main.start_drift_timer" not in _prism_thread_names(mock_t)
    # ...and none of the directly-invoked standing workers ran here either.
    for name, m in mocks.items():
        assert m.call_count == 0, f"{name} ran in-process despite PRISM_WORKERS_PROCESS=1"


def test_workers_process_disabled_keeps_starting_workers_in_process(
        isolated_lock, monkeypatch):
    import prism_service.main as main_mod
    monkeypatch.setenv("PRISM_WORKERS_PROCESS", "0")
    mocks = _patch_directly_called_workers(monkeypatch)

    spawn_calls = []
    monkeypatch.setattr(
        main_mod, "_spawn_worker_host",
        lambda: spawn_calls.append(1) or MagicMock(),
    )

    from unittest.mock import patch
    with patch("prism_service.main.threading.Thread") as mock_t, \
            patch("prism_service.main._install_stackdump_handler"):
        _run_lifespan()

    assert len(spawn_calls) == 0
    assert "prism_service.main.start_drift_timer" in _prism_thread_names(mock_t)
    for name, m in mocks.items():
        assert m.call_count == 1, f"{name} did not run in-process with PRISM_WORKERS_PROCESS=0"


def test_lifespan_teardown_terminates_the_worker_host_process(
        isolated_lock, monkeypatch):
    import prism_service.main as main_mod
    monkeypatch.setenv("PRISM_WORKERS_PROCESS", "1")
    _patch_directly_called_workers(monkeypatch)

    fake_proc = MagicMock()
    fake_proc.pid = 424242
    fake_proc.is_alive.return_value = True
    monkeypatch.setattr(main_mod, "_spawn_worker_host", lambda: fake_proc)

    from unittest.mock import patch
    with patch("prism_service.main.threading.Thread"), \
            patch("prism_service.main._install_stackdump_handler"):
        _run_lifespan()

    fake_proc.terminate.assert_called_once()
    fake_proc.join.assert_called_once()


# ---------------------------------------------------------------------------
# worker_host.start_all_workers(): the CHILD process actually starts every
# standing worker.
# ---------------------------------------------------------------------------

def test_worker_host_starts_every_standing_worker(monkeypatch):
    from prism_service.services import worker_host
    mocks = _patch_directly_called_workers(monkeypatch)

    from unittest.mock import patch
    import prism_service.main as main_mod
    drift_mock = MagicMock()
    monkeypatch.setattr(main_mod, "start_drift_timer", drift_mock)

    with patch("prism_service.services.worker_host.threading.Thread") as mock_t:
        worker_host.start_all_workers()

    for name, m in mocks.items():
        assert m.call_count == 1, f"worker host never started {name}"
    # drift is thread-wrapped, not called directly
    drift_thread_targets = [
        c.kwargs.get("target") or (c.args[0] if c.args else None)
        for c in mock_t.call_args_list
    ]
    assert drift_mock in drift_thread_targets


def test_worker_host_names_the_nine_standing_workers():
    from prism_service.services import worker_host
    names = worker_host.worker_names()
    assert set(names) == {
        "task_runner", "gate_adjudicator", "resume_actuator",
        "deploy_worker", "dispatch_guard", "language_alignment_worker",
        "maintenance_clock", "ship_worker", "drift_timer",
    }
