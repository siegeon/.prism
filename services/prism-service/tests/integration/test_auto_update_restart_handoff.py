"""Integration: the apply -> restart handoff actually FLIPS the running version.

Task bc9b1a88 — "Auto-update must actually flip the running version" (5th
recurrence). The defect: after a successful auto-apply the new wheel lands on
disk but the live daemon keeps serving the OLD version forever, because the
updater hard-defers the restart on all platforms (_DEFER_RESTART=True) and
deliberately never restarts (auto_updater._maybe_apply comment "we deliberately
do NOT self-restart").

These tests pin the SAFE restart design the task mandates:

  * After a successful apply, a RESTART REQUEST is raised that the MAIN thread
    can observe (auto_updater.request_restart() / restart_requested()), NOT an
    in-thread execvp from the auto-updater daemon thread (issue #66 guard
    stays: the daemon thread must NEVER call os.execv / os.execvp).
  * The main thread performs the restart via a graceful uvicorn stop followed
    by os.execv — and ONLY from the main thread.
  * The end-to-end effect is that the SERVED version flips: a simulated
    apply -> restart handoff replaces the process such that /api/version would
    report the NEW version with no manual intervention.
  * apply_update().restart_auto truthfully reflects that an automatic restart
    WILL happen (it is no longer hard-wired to False via `not _DEFER_RESTART`).

They FAIL today (red) because none of request_restart / restart_requested /
perform_restart exist, _DEFER_RESTART is True, and restart_auto is always False.
"""

from __future__ import annotations

import threading

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
        au._state.restart_required = False


def _reset_state():
    with au._state_lock:
        au._state.in_docker = False
        au._state.update_available = False
        au._state.asset_url = None
        au._state.latest_version = None
        au._state.restart_required = False
    # Clear any restart request raised during a test.
    clear = getattr(au, "clear_restart_request", None)
    if callable(clear):
        clear()


# ---------------------------------------------------------------------------
# The restart-coordination seam must exist (main-thread handoff, not in-thread
# execvp). These are the contract the main thread polls.
# ---------------------------------------------------------------------------

def test_restart_request_seam_exists():
    """The updater must expose a way to REQUEST a restart (raise a flag) and a
    way for the main thread to OBSERVE that request — the handoff that replaces
    the unsafe in-thread execvp."""
    assert hasattr(au, "request_restart"), \
        "auto_updater must expose request_restart() for the main-thread handoff"
    assert hasattr(au, "restart_requested"), \
        "auto_updater must expose restart_requested() for the main thread to poll"


def test_request_restart_sets_observable_flag():
    """request_restart() (callable from the daemon thread) flips the flag the
    main thread polls; it must NOT itself replace the process."""
    _reset_state()
    try:
        assert au.restart_requested() is False
        au.request_restart()
        assert au.restart_requested() is True
    finally:
        _reset_state()


def test_defer_restart_no_longer_hard_true():
    """The whole bug: _DEFER_RESTART=True meant nothing ever restarted. The
    safe design hands the restart to the main thread, so the all-platforms
    hard-defer must be gone."""
    assert getattr(au, "_DEFER_RESTART", False) is not True, \
        "_DEFER_RESTART must no longer be hard-wired True — restart is handed "\
        "to the main thread, not deferred forever"


# ---------------------------------------------------------------------------
# Issue #66 guard MUST survive: the auto-updater DAEMON thread never execvp's.
# But after a successful apply it MUST raise a restart request.
# ---------------------------------------------------------------------------

def test_maybe_apply_requests_restart_without_self_exec(monkeypatch):
    """apply -> restart handoff, asserted on the daemon thread:
      (a) NO os.execv / os.execvp is invoked on the auto-updater thread (#66),
      (b) a restart REQUEST is raised so the main thread can flip the version.
    """
    called = {"execvp": False, "execv": False}
    monkeypatch.setattr(au.os, "execvp",
                        lambda *a, **k: called.__setitem__("execvp", True))
    monkeypatch.setattr(au.os, "execv",
                        lambda *a, **k: called.__setitem__("execv", True))
    monkeypatch.setattr(au, "_AUTO_APPLY", True)
    monkeypatch.setattr(au, "apply_update",
                        lambda: {"ok": True, "target_version": "9.9.9",
                                 "restart_required": True, "restart_auto": True})
    _arm_update_available()

    # Run _maybe_apply on a NON-main (daemon) thread, mirroring production.
    err = {}

    def _runner():
        try:
            au._maybe_apply()
        except Exception as e:  # pragma: no cover - surfaced via err
            err["e"] = e

    t = threading.Thread(target=_runner, name="prism-auto-updater", daemon=True)
    t.start()
    t.join(timeout=10)
    try:
        assert not err, f"_maybe_apply raised: {err.get('e')!r}"
        assert called["execvp"] is False, "daemon thread must NOT os.execvp (#66)"
        assert called["execv"] is False, "daemon thread must NOT os.execv (#66)"
        assert au.restart_requested() is True, \
            "a successful apply must RAISE a restart request for the main thread"
    finally:
        _reset_state()


# ---------------------------------------------------------------------------
# The MAIN-thread restart performer: graceful uvicorn stop, then os.execv.
# ---------------------------------------------------------------------------

def test_perform_restart_execs_from_main_thread(monkeypatch):
    """The actual process replacement lives in a main-thread performer that
    (1) gracefully stops the running uvicorn server, then (2) os.execv's into
    the new image. We assert it drains the server first and execs after."""
    perform = getattr(au, "perform_restart", None)
    assert callable(perform), \
        "auto_updater must expose perform_restart() — the main-thread re-exec"

    order = []

    class _Server:
        def __init__(self):
            self.should_exit = False

        def stop(self):
            order.append("server-stopped")

    server = _Server()

    def _fake_execv(path, argv):
        order.append("execv")

    monkeypatch.setattr(au.os, "execv", _fake_execv)
    _arm_update_available()
    au.request_restart()
    try:
        # perform_restart should gracefully signal the server to exit, wait
        # for sockets to drain, then execv. We pass the server handle so the
        # graceful-stop path is exercised.
        au.perform_restart(server=server)
    finally:
        _reset_state()

    assert "execv" in order, "perform_restart must os.execv into the new image"
    # Graceful stop must happen BEFORE the exec (sockets closed first, #66).
    if "server-stopped" in order:
        assert order.index("server-stopped") < order.index("execv"), \
            "must drain/stop the server BEFORE os.execv"
    else:
        # If perform_restart drives the server via should_exit instead of a
        # stop() call, the flag must be set before exec.
        assert server.should_exit is True, \
            "must signal uvicorn should_exit before os.execv"


def test_apply_update_restart_auto_is_true(monkeypatch):
    """apply_update()'s restart_auto must reflect that the restart WILL happen
    automatically — not the always-False `not _DEFER_RESTART`. We stub the pip
    install so apply reaches the success return without shelling out."""
    monkeypatch.delenv("PRISM_DEV_MODE", raising=False)
    monkeypatch.delenv("PRISM_AUTO_UPDATE", raising=False)

    class _Resp:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(au.urllib.request, "urlopen", lambda *a, **k: _Resp())

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(au.subprocess, "run", lambda *a, **k: _Proc())
    _arm_update_available()
    try:
        result = au.apply_update()
    finally:
        _reset_state()
    assert result["ok"] is True, result
    assert result["restart_auto"] is True, \
        "restart_auto must be True now that the restart happens automatically"


# ---------------------------------------------------------------------------
# Pidfile must SURVIVE the in-place re-exec. os.execv replaces the process
# image with the SAME PID, so the pidfile that names this PID stays valid —
# and the re-exec'd `-m prism_service.main` never re-writes it. If the
# restart path unlinked the pidfile first, a live daemon (same PID, new
# version) would be left with no pidfile and `prism status`/`prism stop`
# would report it as not running — breaking the bare `prism start --daemon`
# (pidfile manager) acceptance criterion.
# ---------------------------------------------------------------------------

def test_perform_restart_does_not_touch_pidfile(monkeypatch, tmp_path):
    """perform_restart() must NOT unlink the pidfile — execv keeps the PID,
    so the file stays valid across the flip."""
    from prism_service.data_dir import pid_file
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    pf = pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(__import__("os").getpid()), encoding="utf-8")

    monkeypatch.setattr(au.os, "execv", lambda *a, **k: None)
    _arm_update_available()
    au.request_restart()
    try:
        au.perform_restart(server=None)
    finally:
        _reset_state()
    assert pf.exists(), \
        "perform_restart must leave the pidfile in place (execv keeps the PID)"


def test_main_restart_branch_does_not_clear_pidfile():
    """Regression: the main.py auto-update re-exec branch must NOT call
    _own_pidfile_cleanup() before perform_restart(). execv preserves the PID,
    so clearing the pidfile would orphan a live, upgraded daemon from the
    pidfile manager. Asserted against the source so the contract can't silently
    regress (the branch lives under `if __name__ == '__main__'`)."""
    import inspect
    from prism_service import main as main_mod

    src = inspect.getsource(main_mod)
    # Isolate the restart branch (after the 'performing main-thread re-exec'
    # log line) and assert no pidfile cleanup precedes perform_restart there.
    marker = "performing main-thread re-exec"
    assert marker in src, "restart branch log marker missing"
    branch = src[src.index("if _au.restart_requested():"):]
    perform_idx = branch.index("perform_restart()")
    before_perform = branch[:perform_idx]
    assert "_own_pidfile_cleanup()" not in before_perform, \
        "must NOT unlink the pidfile before the in-place execv re-exec"
