"""Unit tests for prism_service.services.auto_updater.

Covers the parts that are pure logic (SemVer comparison, wheel asset
detection, status shape). Doesn't exercise the actual GitHub Releases
call or pip subprocess — those are integration concerns.
"""

from __future__ import annotations

import os

import pytest

from prism_service.services import auto_updater as au


# ---------------------------------------------------------------------------
# _is_newer_semver
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("newer,older", [
    ("v6.0.1", "6.0.0"),
    ("6.0.1", "v6.0.0"),
    ("6.1.0", "6.0.99"),
    ("7.0.0", "6.99.99"),
    ("v6.0.10", "v6.0.9"),
])
def test_strictly_newer_is_detected(newer, older):
    assert au._is_newer_semver(newer, older) is True


@pytest.mark.parametrize("a,b", [
    ("6.0.0", "6.0.0"),
    ("6.0.0", "v6.0.0"),
    ("v6.0.0", "v6.0.0"),
])
def test_equal_is_not_newer(a, b):
    assert au._is_newer_semver(a, b) is False


@pytest.mark.parametrize("older,newer", [
    ("6.0.0", "6.0.1"),
    ("6.0.0", "6.1.0"),
    ("5.99.99", "6.0.0"),
])
def test_older_is_not_newer(older, newer):
    assert au._is_newer_semver(older, newer) is False


def test_malformed_falls_back_to_string_compare():
    # Not a clean SemVer triple — function returns lexical compare.
    # The actual result doesn't matter as much as "doesn't raise."
    result = au._is_newer_semver("not-a-version", "also-not-a-version")
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _wheel_asset_url
# ---------------------------------------------------------------------------

def test_finds_wheel_asset():
    release = {
        "assets": [
            {"name": "prism_service-6.0.1-py3-none-any.whl",
             "browser_download_url": "https://example.com/wheel"},
            {"name": "prism_service-6.0.1.tar.gz",
             "browser_download_url": "https://example.com/sdist"},
        ],
    }
    assert au._wheel_asset_url(release) == "https://example.com/wheel"


def test_returns_none_when_no_wheel():
    release = {
        "assets": [
            {"name": "prism_service-6.0.1.tar.gz",
             "browser_download_url": "https://example.com/sdist"},
        ],
    }
    assert au._wheel_asset_url(release) is None


def test_returns_none_when_no_assets():
    assert au._wheel_asset_url({}) is None
    assert au._wheel_asset_url({"assets": []}) is None


# ---------------------------------------------------------------------------
# Status surface
# ---------------------------------------------------------------------------

def test_get_status_returns_expected_keys():
    s = au.get_status()
    for key in (
        "running_version", "latest_version", "update_available",
        "in_docker", "auto_apply_enabled", "last_check_at",
        "last_check_ok", "last_error", "restart_required",
        "asset_url", "poll_interval_s",
    ):
        assert key in s, f"missing key: {key}"


def test_running_version_matches_package():
    from prism_service.__version__ import PRISM_VERSION
    assert au.get_status()["running_version"] == PRISM_VERSION


def test_apply_update_refuses_when_no_update_available(monkeypatch):
    """Defense: calling apply without a check_for_update should error
    out, not blindly fire pip."""
    # Reset state to a known no-update-available baseline.
    with au._state_lock:
        au._state.update_available = False
        au._state.in_docker = False
        au._state.asset_url = None
    result = au.apply_update()
    assert result["ok"] is False
    assert "no update available" in result["reason"]


def test_apply_update_refuses_in_docker():
    with au._state_lock:
        au._state.in_docker = True
        au._state.update_available = True
        au._state.asset_url = "https://example.com/wheel"
    result = au.apply_update()
    assert result["ok"] is False
    assert "docker" in result["reason"].lower()
    # Cleanup
    with au._state_lock:
        au._state.in_docker = False
        au._state.update_available = False
        au._state.asset_url = None


# ---------------------------------------------------------------------------
# Issue #66 — the auto-updater must never silently kill the daemon
# ---------------------------------------------------------------------------

def test_maybe_apply_never_self_execs(monkeypatch):
    """The actual #66 silent-death cause: _maybe_apply re-exec'd the live
    process via os.execvp from a daemon thread (same PID, no traceback,
    sockets dropped). It must NEVER call execvp on any platform now."""
    called = {"execvp": False}
    monkeypatch.setattr(au.os, "execvp",
                        lambda *a, **k: called.__setitem__("execvp", True))
    monkeypatch.setattr(au, "_AUTO_APPLY", True)
    monkeypatch.setattr(au, "apply_update", lambda: {"ok": True})
    with au._state_lock:
        au._state.update_available = True
        au._state.restart_required = False
        au._state.latest_version = "9.9.9"
    try:
        au._maybe_apply()
    finally:
        with au._state_lock:
            au._state.update_available = False
            au._state.restart_required = False
            au._state.latest_version = None
    assert called["execvp"] is False


def test_self_restart_helper_is_gone():
    """The in-place re-exec helper was removed in #66 — its existence is
    a regression risk, so assert it stays gone."""
    assert not hasattr(au, "_self_restart")


def test_restart_is_handed_to_main_thread_not_deferred_forever():
    """Task bc9b1a88 (5th recurrence): the #66 guard is "never execvp from the
    DAEMON thread" — NOT "never restart at all". The old _DEFER_RESTART=True
    meant nothing ever restarted and the served version never flipped. The safe
    design hands the restart to the MAIN thread, so the all-platforms hard-defer
    must be gone and the request/perform seam must exist."""
    assert getattr(au, "_DEFER_RESTART", False) is not True, \
        "_DEFER_RESTART must no longer be hard True — restart is handed to main"
    assert hasattr(au, "request_restart"), "need request_restart() handoff"
    assert hasattr(au, "restart_requested"), "need restart_requested() poll"
    assert hasattr(au, "perform_restart"), "need perform_restart() main-thread exec"


def test_stale_self_exec_docstring_is_corrected():
    """The api/update.py apply() docstring used to LIE: 'On Linux/Mac the
    auto-updater self-execs after success'. No such self-exec ever existed, and
    the new design re-execs from the MAIN thread on every platform. The stale
    Linux/Mac claim must be gone."""
    from prism_service.api import update as update_api
    doc = update_api.apply.__doc__ or ""
    assert "self-execs after success" not in doc, \
        "stale Linux/Mac self-exec claim must be removed from apply() docstring"


def test_auto_apply_default_on_opt_out():
    """v6.2.7: PRISM_AUTO_UPDATE defaults ON (opt-out) — auto-update was
    silently broken while it defaulted off (v6.2.4-6.2.6). The #66
    silent-death cause was the os.execvp self-restart (still deferred,
    see test above), not the apply, so default-on is safe."""
    def _eval(env_val):
        raw = (env_val if env_val is not None else "on").lower()
        return raw in ("on", "true", "1", "yes")
    assert _eval(None) is True            # unset -> on
    assert _eval("off") is False          # explicit opt-out honored
    assert _eval("false") is False
    assert _eval("on") is True
    assert _eval("1") is True
    # And the module honored the default at import time (env unset in CI).
    if not os.environ.get("PRISM_AUTO_UPDATE"):
        assert au._AUTO_APPLY is True


# ---------------------------------------------------------------------------
# Dev-mode guard (task 56d258ae) — the source-run dev instance must NEVER
# pip-install over itself. PRISM_DEV_MODE truthy => apply path short-circuits
# exactly like _running_in_docker() does, and subprocess.run is never invoked.
# The version CHECK path stays live; only the apply/pip path is blocked.
# An explicit PRISM_AUTO_UPDATE=on still force-enables apply in dev.
# ---------------------------------------------------------------------------

def _arm_update_available():
    """Put _state in the 'a wheel is ready to install' shape used by the
    dangerous apply path, with docker off so only the dev guard can stop it."""
    with au._state_lock:
        au._state.in_docker = False
        au._state.update_available = True
        au._state.asset_url = "https://example.com/prism_service-9.9.9-py3-none-any.whl"
        au._state.latest_version = "9.9.9"
        au._state.restart_required = False


def _reset_state():
    with au._state_lock:
        au._state.in_docker = False
        au._state.update_available = False
        au._state.asset_url = None
        au._state.latest_version = None
        au._state.restart_required = False


def _guard_subprocess(monkeypatch):
    """Trip-wire: any call to the installer subprocess is a hard failure.
    Returns the call-counter dict so a test can also assert count == 0."""
    calls = {"run": 0}

    def _boom(*a, **k):
        calls["run"] += 1
        raise AssertionError(
            "subprocess.run was invoked in dev mode — the pip-install path "
            "must be blocked when PRISM_DEV_MODE is set"
        )

    monkeypatch.setattr(au.subprocess, "run", _boom)
    return calls


def _stub_download(monkeypatch):
    """Neuter the wheel download so a test that wrongly proceeds past the
    dev guard fails on the subprocess trip-wire (the real seam) rather than
    on a network error. Writes empty bytes to whatever path apply picks."""
    class _Resp:
        def read(self):
            return b""
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(au.urllib.request, "urlopen", lambda *a, **k: _Resp())


def test_dev_mode_helper_exists_and_reads_env(monkeypatch):
    """The guard mirrors api/version._dev_mode env semantics:
    1/true/yes/on (case-insensitive) are truthy; everything else false."""
    assert hasattr(au, "_dev_mode"), "auto_updater must expose a _dev_mode() guard"
    for truthy in ("1", "true", "TRUE", "yes", "on", "On"):
        monkeypatch.setenv("PRISM_DEV_MODE", truthy)
        assert au._dev_mode() is True, f"{truthy!r} should be dev mode"
    for falsy in ("", "0", "off", "no", "false"):
        monkeypatch.setenv("PRISM_DEV_MODE", falsy)
        assert au._dev_mode() is False, f"{falsy!r} should not be dev mode"


def test_apply_update_refuses_in_dev_mode_and_never_runs_subprocess(monkeypatch):
    """PRISM_DEV_MODE=1, no PRISM_AUTO_UPDATE override: apply_update() must
    short-circuit with a 'dev mode' reason and NEVER shell out to pip."""
    monkeypatch.setenv("PRISM_DEV_MODE", "1")
    monkeypatch.delenv("PRISM_AUTO_UPDATE", raising=False)
    calls = _guard_subprocess(monkeypatch)
    _stub_download(monkeypatch)
    _arm_update_available()
    try:
        result = au.apply_update()
    finally:
        _reset_state()
    assert result["ok"] is False
    assert "dev mode" in result["reason"].lower(), result
    assert calls["run"] == 0


def test_maybe_apply_no_ops_in_dev_mode(monkeypatch):
    """The background loop's _maybe_apply() must take NO action in dev mode:
    apply_update is never reached and the installer subprocess never fires."""
    monkeypatch.setenv("PRISM_DEV_MODE", "1")
    monkeypatch.delenv("PRISM_AUTO_UPDATE", raising=False)
    calls = _guard_subprocess(monkeypatch)
    _stub_download(monkeypatch)
    applied = {"called": False}
    monkeypatch.setattr(au, "apply_update",
                        lambda: applied.__setitem__("called", True) or {"ok": True})
    _arm_update_available()
    try:
        au._maybe_apply()
    finally:
        _reset_state()
    assert applied["called"] is False, "apply_update must not run in dev mode"
    assert calls["run"] == 0


def test_explicit_auto_update_on_overrides_dev_mode(monkeypatch):
    """Escape hatch: PRISM_AUTO_UPDATE=on force-enables apply even in dev,
    so the apply path proceeds past the dev guard (it does NOT short-circuit
    with a 'dev mode' reason). We stop before the real pip call by raising a
    sentinel from the installer, proving control reached the install path."""
    monkeypatch.setenv("PRISM_DEV_MODE", "1")
    monkeypatch.setenv("PRISM_AUTO_UPDATE", "on")
    _stub_download(monkeypatch)

    class _Sentinel(Exception):
        pass

    def _reached(*a, **k):
        raise _Sentinel()

    monkeypatch.setattr(au.subprocess, "run", _reached)
    _arm_update_available()
    try:
        with pytest.raises(_Sentinel):
            au.apply_update()
    finally:
        _reset_state()


def test_check_for_update_still_runs_in_dev_mode(monkeypatch):
    """Only the apply/pip path is gated. The version CHECK that feeds the
    SPA 'update available' banner must still execute in dev mode."""
    monkeypatch.setenv("PRISM_DEV_MODE", "1")
    monkeypatch.delenv("PRISM_AUTO_UPDATE", raising=False)
    fake_release = {
        "tag_name": "v99.0.0",
        "published_at": "2099-01-01T00:00:00Z",
        "assets": [{"name": "prism_service-99.0.0-py3-none-any.whl",
                    "browser_download_url": "https://example.com/w.whl"}],
    }
    monkeypatch.setattr(au, "_fetch_latest_release", lambda: fake_release)
    try:
        state = au.check_for_update()
        assert state.last_check_ok is True
        assert state.latest_version == "v99.0.0"
        assert state.update_available is True
    finally:
        _reset_state()


def test_api_update_apply_returns_409_dev_mode_through_real_route(monkeypatch):
    """USER-FACING INTEGRATION: POST /api/update/apply must surface the dev
    guard through the real FastAPI dispatcher — a 409 whose detail names
    'dev mode' — and the installer subprocess must never run. This pins the
    seam end-to-end (route -> apply_update -> 409), not just a method call."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api.update import router as update_router

    monkeypatch.setenv("PRISM_DEV_MODE", "1")
    monkeypatch.delenv("PRISM_AUTO_UPDATE", raising=False)
    calls = _guard_subprocess(monkeypatch)
    _stub_download(monkeypatch)
    _arm_update_available()

    app = FastAPI()
    app.include_router(update_router, prefix="/api/update")
    client = TestClient(app)
    try:
        resp = client.post("/api/update/apply")
    finally:
        _reset_state()
    assert resp.status_code == 409, resp.text
    assert "dev mode" in resp.json().get("detail", "").lower(), resp.text
    assert calls["run"] == 0


# ---------------------------------------------------------------------------
# AC#4 — the prism-dev launch recipe must also belt-and-suspenders disable
# auto-apply (PRISM_AUTO_UPDATE=off), kill the poll loop (interval=0), and
# ignore the user-site shadow (PYTHONNOUSERSITE=1). The dev-mode guard is the
# default, these are the explicit reinforcement the skill recipe carries.
# ---------------------------------------------------------------------------

def _prism_dev_skill_text():
    from pathlib import Path
    here = Path(__file__).resolve()
    # tests/unit/test_auto_updater.py -> repo root is parents[4] (E:\.prism)
    for anc in here.parents:
        candidate = anc / ".claude" / "skills" / "prism-dev" / "SKILL.md"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise AssertionError("could not locate .claude/skills/prism-dev/SKILL.md")


def test_prism_dev_recipe_exports_auto_update_off():
    text = _prism_dev_skill_text()
    assert 'PRISM_AUTO_UPDATE' in text and '"off"' in text, \
        "launch recipe must export PRISM_AUTO_UPDATE=off"


def test_prism_dev_recipe_zeroes_update_interval():
    text = _prism_dev_skill_text()
    assert 'PRISM_AUTO_UPDATE_INTERVAL' in text and '"0"' in text, \
        "launch recipe must export PRISM_AUTO_UPDATE_INTERVAL=0"


def test_prism_dev_recipe_sets_no_usersite():
    text = _prism_dev_skill_text()
    assert 'PYTHONNOUSERSITE' in text and '"1"' in text, \
        "launch recipe must export PYTHONNOUSERSITE=1"
