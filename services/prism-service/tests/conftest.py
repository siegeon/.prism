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


@pytest.fixture(autouse=True)
def _pin_reranker_off():
    """Tests must not inherit whether THIS machine can run a cross-encoder.

    PRISM_RERANK defaults to ``auto`` (task 19e4e7f7): on wherever
    sentence-transformers is installed, off where it is not. That is right for
    a product and wrong for a suite -- it would make every search test load a
    cross-encoder on a dev box with the [neural] extra and skip it in CI,
    so the same assertion would measure two different pipelines. Pin it off;
    the tests that are ABOUT reranking set it themselves.
    """
    prior = os.environ.get("PRISM_RERANK")
    os.environ["PRISM_RERANK"] = "off"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("PRISM_RERANK", None)
        else:
            os.environ["PRISM_RERANK"] = prior


@pytest.fixture(autouse=True)
def _integration_adapter_registry_isolation():
    """Snapshot/restore api.integrations._adapters around every test
    (task c23e2e7b). That registry is a module-level global that
    production populates once at import time, and tests reach into it
    directly (reset_adapters/register_adapter) to script a scenario.
    Running tests/integration/test_external_work_sync.py then
    tests/integration/test_pull_issues_into_tasks.py in one process used
    to leak: the first file's `team` fixture cleared the registry at
    teardown and never put back what was there before it ran, so the
    second file saw no github adapter. Because this fixture lives in the
    root conftest, pytest sets it up before any test-module-local fixture
    and tears it down after, so no test - this suite, a neighbour, or one
    written next month - can leave a registry mutation for the next test
    to inherit."""
    from prism_service.api import integrations

    before = integrations.adapters_snapshot()
    try:
        yield
    finally:
        integrations.restore_adapters(before)


@pytest.fixture
def quiet_boot(monkeypatch):
    """Boot the REAL lifespan (prism_service.main.app) without its periodic
    workers starting.

    Shared home (task 0784729f): originally local to
    test_task_mirror_production_wiring.py, moved here so every unit test
    that needs `with TestClient(app) as client:` on the ACTUAL production
    app - to prove a route or registration is really wired, not merely
    reachable on a bare test FastAPI() - gets the same protection instead
    of re-deriving it. Skipping this is not cosmetic: a unit test that boots
    the real lifespan and lets its ~10 daemon threads run leaves them alive
    for the REST of the pytest process (main.py's shutdown only stops the
    watchdog + sqlite maintenance task, nothing else). The leaked
    maintenance-clock thread's first tick fires almost immediately (no
    prior-run timestamp gates it) and shells out real `git` subprocesses,
    whose Popen.communicate() spawns reader threads via threading.Thread -
    which test_lifespan_lock_recovery.py's mock of the GLOBAL
    threading.Thread class then miscounts as its OWN lifespan's threads
    (assert 40 == 10 / assert 44 == 10, task 0784729f). Setting the
    intervals to 0 was NOT enough on its own: the threads still START, and
    on Linux CI this once hung the suite for 26 minutes with a whole
    process full of worker threads still doing their own SQLite work.
    """
    for name, value in (("PRISM_WATCHDOG", "off"),
                        ("PRISM_DRIFT_INTERVAL", "0"),
                        ("PRISM_GATE_ADJUDICATOR_INTERVAL", "0"),
                        ("PRISM_EVENT_POOL_INTERVAL", "0"),
                        ("PRISM_GRAPH_ENRICH_WORKER", "off"),
                        ("PRISM_SQLITE_MAINT_INTERVAL_S", "0")):
        monkeypatch.setenv(name, value)

    # main.py registers whatever the FIRST try/except does (e.g. the task
    # mirror observer) BEFORE the big `try:` that starts every worker - so
    # failing that block's very first statement (`_LOCK_FILE.write_text`)
    # skips all nine-plus starters (MCP server, drift timer, understand
    # drainer, trash sweeper, auto-updater, transcript importer,
    # maintenance clock, watchdog, gate adjudicator) while anything wired
    # ahead of it still runs, and the app is still fully assembled and
    # routable through TestClient.
    class _RefuseLock:
        def exists(self) -> bool:
            return False

        def write_text(self, *_a, **_k):
            raise OSError("workers deliberately not started under test")

        def unlink(self, *_a, **_k) -> None:
            return None

    monkeypatch.setattr("prism_service.main._LOCK_FILE", _RefuseLock())


@pytest.fixture(autouse=True)
def _collaboration_registry_isolation():
    """Same guarantee for the COLLABORATION registry (epic 0784729f).

    services/collaboration.py carries a module-level registry that
    api/collaboration.py populates once at import time, exactly like the
    work-item one above. It is given the same snapshot/restore fixture on
    day one rather than after an incident: the leak that produced the
    fixture above (task c23e2e7b) was latent for months and only detonated
    when file ordering changed, and a registry whose emptiness is invisible
    until some later test expects the production adapter is precisely how
    this epic shipped a feature that could not run (task f4dd3687)."""
    from prism_service.services import collaboration

    before = collaboration.adapters_snapshot()
    try:
        yield
    finally:
        after = collaboration.adapters_snapshot()
        # The builtin adapters register when api/collaboration is IMPORTED,
        # which may happen for the first time DURING a test (anything that
        # imports prism_service.main). Blindly restoring a snapshot taken
        # before that import wiped github for every later test - the same
        # leak this fixture family exists to prevent (c23e2e7b), just with
        # the roles reversed. So only restore when there is something to
        # restore, or when the test left the registry no better than it
        # found it; never erase what production populated mid-test.
        #
        # Importing the api module here instead was the obvious fix and is
        # WRONG: it drags the whole API package into every test and starts
        # its background threads, which broke the lifespan thread-count
        # assertions (test_lifespan_lock_recovery: assert 30 == 10).
        if before or not after:
            collaboration.restore_adapters(before)
