"""Integration (RED): auto-update must not strand the daemon on a blocked drain.

Task 73ec7273 — "Auto-update no longer strands the daemon on the old build"
(lineage of GH #66). The customer (native venv, persistent MCP streamable-HTTP
sessions) stayed on v6.5.0 while 6.5.2/6.5.3 were published. Root cause: the
restart watcher sets `_server.should_exit = True` but NEVER `force_exit`, so
uvicorn's graceful drain blocks forever on a held-open MCP connection — the
main-thread `.run()` never returns, the `os.execv` flip is never reached, and
the served version is stranded on the old build. `perform_restart`'s
`drain_timeout_s` parameter is dead code (auto_updater.py:125-156).

These tests pin the BOUNDED-drain fix and FAIL today (red) because:
  * perform_restart ignores drain_timeout_s and never sets force_exit;
  * the SPA banner is passive ("run prism stop && prism start") not a loud
    one-click "update ready — click to restart" control.

#66 invariants that MUST stay green (asserted here too): the os.execv flip
stays on the MAIN thread and the pidfile is not unlinked before the in-place
execv.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from prism_service.services import auto_updater as au


def _arm_update_available():
    with au._state_lock:
        au._state.in_docker = False
        au._state.update_available = True
        au._state.asset_url = (
            "https://example.com/prism_service-9.9.9-py3-none-any.whl"
        )
        au._state.latest_version = "9.9.9"
        au._state.restart_required = True


def _reset_state():
    with au._state_lock:
        au._state.in_docker = False
        au._state.update_available = False
        au._state.asset_url = None
        au._state.latest_version = None
        au._state.restart_required = False
    clear = getattr(au, "clear_restart_request", None)
    if callable(clear):
        clear()


class _StuckServer:
    """A uvicorn-Server stand-in whose graceful drain NEVER completes — it
    models a held-open MCP streamable-HTTP connection. should_exit alone does
    NOT make run() return; only force_exit does (matches uvicorn semantics)."""

    def __init__(self):
        # Seed flags without tripping the recorder.
        object.__setattr__(self, "events", [])
        object.__setattr__(self, "_done", threading.Event())
        object.__setattr__(self, "should_exit", False)
        object.__setattr__(self, "force_exit", False)

    def __setattr__(self, name, value):
        # Record the FIRST flip of should_exit / force_exit to True so the
        # test can assert escalation ordering against real attribute writes.
        if name in ("should_exit", "force_exit") and value and name not in self.events:
            self.events.append(name)
        object.__setattr__(self, name, value)

    def run(self):
        # Block until force_exit is set, like uvicorn draining a stuck conn.
        while not self.force_exit:
            time.sleep(0.01)
        self._done.set()

    def stop(self):
        # A bare should_exit/stop must NOT unblock a held-open connection.
        self.events.append("stop")


# ---------------------------------------------------------------------------
# AC-1/AC-2/AC-3: perform_restart honors drain_timeout_s, escalates
# should_exit -> force_exit after the timeout, then os.execv — BOUNDED, not
# hung on a clean drain.
# ---------------------------------------------------------------------------

def test_perform_restart_bounds_drain_then_force_exits(monkeypatch):
    """With a held-open (non-draining) connection, perform_restart must NOT
    hang: after drain_timeout_s it sets _server.force_exit so uvicorn drops the
    connection, then os.execv fires. Ordering must be graceful-first:
    should_exit BEFORE force_exit BEFORE execv (no in-flight-write force-kill).
    """
    server = _StuckServer()

    def _fake_execv(path, argv):
        server.events.append("execv")

    monkeypatch.setattr(au.os, "execv", _fake_execv)
    _arm_update_available()
    au.request_restart()

    # Drive perform_restart on a background thread so we can prove it RETURNS
    # within the bounded window even though the server drain never completes.
    drain_timeout_s = 0.5
    err = {}

    def _runner():
        try:
            au.perform_restart(server=server, drain_timeout_s=drain_timeout_s)
        except Exception as e:  # pragma: no cover - surfaced via err
            err["e"] = e

    t = threading.Thread(target=_runner, name="perform-restart", daemon=True)
    started = time.monotonic()
    t.start()
    # Generous ceiling: bounded drain (0.5s) + escalation slack. If the param
    # is dead and the code waits on a clean drain, this join times out.
    t.join(timeout=drain_timeout_s + 4.0)
    elapsed = time.monotonic() - started
    try:
        assert not err, f"perform_restart raised: {err.get('e')!r}"
        assert not t.is_alive(), (
            "perform_restart hung on the stuck drain — drain_timeout_s is dead; "
            "force_exit was never set so .run()/drain never returned"
        )
        assert server.force_exit is True, (
            "after drain_timeout_s elapsed, _server.force_exit must be set so "
            "uvicorn drops the held-open connection"
        )
        assert "execv" in server.events, "os.execv must fire after the bounded drain"
        # graceful-first then force-after-timeout ordering (likely_misfire d).
        assert "should_exit" in server.events, (
            "should_exit must be recorded before the force escalation"
        )
        assert server.events.index("should_exit") < server.events.index("execv")
        if "force_exit" in server.events:
            assert (
                server.events.index("should_exit")
                < server.events.index("force_exit")
                < server.events.index("execv")
            ), "ordering must be should_exit -> force_exit -> execv"
        # The drain must have been BOUNDED by the timeout, not instant and not
        # unbounded: it should take at least ~drain_timeout_s (graceful window)
        # but finish well under the join ceiling.
        assert elapsed >= drain_timeout_s * 0.5, (
            "force_exit fired before honoring the graceful drain window — "
            "in-flight writes could be lost (likely_misfire d)"
        )
    finally:
        _reset_state()


# ---------------------------------------------------------------------------
# AC-4 (#66 invariant): the bounded-drain escalation must NOT re-introduce an
# off-main-thread execv, and must NOT unlink the pidfile before the in-place
# execv. We call perform_restart on the MAIN thread (this test thread) and
# assert execv fires here with the pidfile intact.
# ---------------------------------------------------------------------------

def test_bounded_restart_execs_on_caller_thread_keeps_pidfile(monkeypatch, tmp_path):
    """The force-after-timeout escalation must keep the os.execv on the calling
    (main) thread — no helper thread re-execs (GH #66) — and must leave the
    pidfile in place (execv preserves the PID)."""
    from prism_service.data_dir import pid_file

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    pf = pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(__import__("os").getpid()), encoding="utf-8")

    caller_thread = threading.current_thread().name
    seen = {}

    def _fake_execv(path, argv):
        seen["thread"] = threading.current_thread().name

    monkeypatch.setattr(au.os, "execv", _fake_execv)
    _arm_update_available()
    au.request_restart()
    try:
        # server=None: main.py already ran the server to completion; the
        # bounded path still must execv on THIS thread and keep the pidfile.
        au.perform_restart(server=None, drain_timeout_s=0.2)
    finally:
        _reset_state()

    assert seen.get("thread") == caller_thread, (
        "os.execv must run on the caller's (main) thread — an off-main-thread "
        "re-exec reintroduces GH #66"
    )
    assert pf.exists(), (
        "perform_restart must leave the pidfile in place across the in-place execv"
    )


# ---------------------------------------------------------------------------
# AC-5: GET /api/update/status surfaces restart_required (the field the SPA
# polls to know an update is ready). Mount the real router, hit the real route.
# ---------------------------------------------------------------------------

def test_api_update_status_surfaces_restart_required():
    """The /api/update/status route must return restart_required in its JSON
    body — proven through the real FastAPI router + TestClient, not a direct
    get_status() call (the field must survive serialization on the live seam)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api.update import router as update_router

    app = FastAPI()
    app.include_router(update_router, prefix="/api/update")
    _arm_update_available()  # sets restart_required=True
    try:
        with TestClient(app) as client:
            resp = client.get("/api/update/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "restart_required" in body, (
            "GET /api/update/status must surface restart_required for the SPA"
        )
        assert body["restart_required"] is True
    finally:
        _reset_state()


# ---------------------------------------------------------------------------
# AC-6: SettingsPage.tsx must render a LOUD 'update ready — click to restart'
# control when restart_required is true — not just the passive informational
# banner ("run prism stop && prism start"). Asserted against the source so the
# user-facing surface can't silently regress.
# ---------------------------------------------------------------------------

def test_settings_page_shows_loud_click_to_restart_control():
    """When restart_required is true the SPA must offer a one-click restart
    control (turning a slow/blocked drain into a user action), not only a
    passive 'restart required' notice."""
    spa = (
        Path(__file__).resolve().parents[2]
        / "prism_service" / "web" / "src" / "pages" / "SettingsPage.tsx"
    )
    src = spa.read_text(encoding="utf-8")
    assert "restart_required" in src, "SettingsPage must read restart_required"
    lowered = src.lower()
    # A loud, actionable 'update ready — click to restart' control: an onClick
    # restart handler gated on restart_required, not just static prose.
    assert "click to restart" in lowered, (
        "SettingsPage must show a loud 'update ready — click to restart' "
        "control when restart_required is true (one-click action, not a "
        "passive 'run prism stop' notice)"
    )
