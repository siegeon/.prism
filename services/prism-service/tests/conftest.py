"""Root test conftest — data-dir isolation for the whole suite.

WHY THIS RUNS AT IMPORT TIME (not as a plain fixture):
prism_service.config resolves ``DATA_DIR = resolve_data_dir()`` as a
module-level constant at IMPORT time, and test modules import the app /
services during pytest collection — so a function-scoped
``monkeypatch.setenv`` fixture fires far TOO LATE to win. The one seam
pytest guarantees runs before any test module in this tree is imported
is this conftest's own import, so the env pin lives at module scope.

Verified empirically (2026-07-03): without this file, running
tests/integration/test_phase_progress_seam.py against a scratch default
data dir created one junk project per test under <data>/projects/
(ConductorService derives a project id from the scores.db parent dir
name — the pytest tmp_path basename — and feeds it to
config.project_data_dir, which mkdirs under the LIVE store). That is
exactly how 15+ junk projects (test_conductor_state_api_carri0, ...)
leaked into the months-long dogfood store.

Opt-out: mark a test ``@pytest.mark.live_data`` to suspend the pin for
its duration — for tests that INTENTIONALLY target a live daemon's
store via subprocesses or call-time resolve_data_dir(). No such tests
exist today (the suite has no live-:8888 HTTP tests and no prior
marker convention); the marker is registered here so future ones plug
into this seam instead of inventing a parallel one. NOTE: in-process
module constants (config.DATA_DIR) stay frozen at first import
regardless — live_data only affects call-time resolution and child
processes.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

import pytest

_ENV = "PRISM_DATA_DIR"
_LIVE_MARKER = "live_data"

# What the developer's shell had before the suite pinned it (None = unset).
_ORIGINAL_DATA_DIR = os.environ.get(_ENV)


def _pin_data_dir() -> str:
    """Point PRISM_DATA_DIR at a fresh throwaway dir, before any
    prism_service import can freeze the default into config.DATA_DIR."""
    root = tempfile.mkdtemp(prefix="prism-test-data-")
    os.environ[_ENV] = root
    return root


def _repoint_config_if_already_imported(root: str) -> None:
    """Belt-and-braces: if some plugin imported prism_service.config before
    this conftest, its constants froze against the REAL default. Re-point
    them in place so every later ``from prism_service.config import X``
    binds the isolated paths. (Modules that already from-imported the old
    values keep stale bindings — nothing fixes that retroactively — so we
    warn loudly instead of failing silently.)"""
    cfg = sys.modules.get("prism_service.config")
    if cfg is None:
        return
    import warnings
    from pathlib import Path

    warnings.warn(
        "prism_service.config was imported before tests/conftest.py pinned "
        f"{_ENV}; re-pointing DATA_DIR/PROJECTS_DIR in place. Check for a "
        "pytest plugin importing prism_service at startup.",
        RuntimeWarning,
        stacklevel=2,
    )
    cfg.DATA_DIR = Path(root)
    cfg.PROJECTS_DIR = cfg.DATA_DIR / "projects"
    if "PRISM_PROJECT_DIR" not in os.environ:
        cfg.PROJECT_DIR = cfg.DATA_DIR


TEST_DATA_DIR = _pin_data_dir()
_repoint_config_if_already_imported(TEST_DATA_DIR)
# Windows note: sqlite handles can outlive the session; best-effort cleanup.
atexit.register(shutil.rmtree, TEST_DATA_DIR, ignore_errors=True)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{_LIVE_MARKER}: test intentionally targets the live PRISM data dir "
        "or a running daemon; the session-wide PRISM_DATA_DIR pin is "
        "suspended (env restored to the pre-session value) for its duration.",
    )


@pytest.fixture(autouse=True)
def _prism_data_dir_isolation(request):
    """Keep every test pinned to the throwaway data dir; suspend the pin
    only for tests explicitly marked @pytest.mark.live_data."""
    if request.node.get_closest_marker(_LIVE_MARKER) is None:
        yield
        return
    if _ORIGINAL_DATA_DIR is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = _ORIGINAL_DATA_DIR
    try:
        yield
    finally:
        os.environ[_ENV] = TEST_DATA_DIR
