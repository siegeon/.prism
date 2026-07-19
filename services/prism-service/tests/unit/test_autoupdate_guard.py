"""The auto-updater must not cut a customer over to a build whose workflow
they cannot complete (owner 2026-07-18, task e5fbec61).

A release is APPLIED only if it carries the workability marker that release CI
attaches after the workflow self-check passes; otherwise the guard refuses and
the client stays on its current version. Default = blocked.
"""
import copy

import pytest

from prism_service.services import auto_updater as au


@pytest.fixture(autouse=True)
def _restore_state():
    """Snapshot/restore the module-global _state so tests don't bleed."""
    with au._state_lock:
        snap = copy.deepcopy(au._state)
    yield
    with au._state_lock:
        au._state.__dict__.update(snap.__dict__)


def test_marker_in_body_is_workable():
    ok, _ = au._release_is_workable({"body": "notes\nPRISM-WORKABLE\nmore"})
    assert ok is True


def test_marker_asset_is_workable():
    ok, _ = au._release_is_workable({"assets": [{"name": "prism-workable.txt"}]})
    assert ok is True


def test_no_marker_is_not_workable():
    ok, reason = au._release_is_workable({"body": "just notes", "assets": []})
    assert ok is False
    assert "marker" in reason.lower()


def test_prerelease_never_workable():
    ok, reason = au._release_is_workable({"prerelease": True,
                                          "body": "PRISM-WORKABLE"})
    assert ok is False
    assert "prerelease" in reason.lower()


def test_apply_refuses_unmarked_release(monkeypatch):
    """A newer, wheel-carrying release WITHOUT the marker is refused BEFORE any
    download/install — the reason names workability, not a network failure."""
    monkeypatch.setenv("PRISM_UPDATE_REQUIRE_WORKABLE", "on")
    monkeypatch.setattr(au, "_dev_mode_blocks_apply", lambda: False)
    with au._state_lock:
        au._state.in_docker = False
        au._state.update_available = True
        au._state.asset_url = "http://example.invalid/prism-9.9.9.whl"
        au._state.latest_version = "v9.9.9"
        au._state.latest_workable = False
        au._state.workable_reason = "no workability marker"
        au._state.restart_required = False
    res = au.apply_update()
    assert res["ok"] is False
    assert "workable" in res["reason"].lower()


def test_apply_allows_marked_release_past_the_guard(monkeypatch):
    """With the marker present the workability guard does not fire; the apply
    proceeds to the download stage (which we stub) instead of refusing."""
    monkeypatch.setenv("PRISM_UPDATE_REQUIRE_WORKABLE", "on")
    monkeypatch.setattr(au, "_dev_mode_blocks_apply", lambda: False)
    with au._state_lock:
        au._state.in_docker = False
        au._state.update_available = True
        au._state.asset_url = "http://example.invalid/prism-9.9.9.whl"
        au._state.latest_version = "v9.9.9"
        au._state.latest_workable = True
        au._state.workable_reason = "marker in body"
        au._state.restart_required = False
    res = au.apply_update()
    # Past the guard: it fails on the (stubbed/unreachable) DOWNLOAD, never on
    # workability — proving the marker let it through the guard.
    assert res["ok"] is False
    assert "workable" not in res["reason"].lower()


def test_guard_bypass_env_off(monkeypatch):
    monkeypatch.setenv("PRISM_UPDATE_REQUIRE_WORKABLE", "off")
    assert au._require_workable() is False


def test_guard_on_by_default(monkeypatch):
    monkeypatch.delenv("PRISM_UPDATE_REQUIRE_WORKABLE", raising=False)
    assert au._require_workable() is True
