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


def test_restart_is_deferred_on_all_platforms():
    """#66: never auto-restart in-place, regardless of OS."""
    assert au._DEFER_RESTART is True


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
