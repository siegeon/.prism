"""Trash sweeper: once-only "still locked" logging.

The old sweep_once() logged on every pass — every 30s for the lifetime
of the process if the file stayed locked (Windows + open SQLite handle).
This test pins the new behavior: log once on first failure, stay quiet,
log once again on success.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from prism_service import config
from prism_service.services import trash


@pytest.fixture
def isolated_projects_root(tmp_path, monkeypatch):
    proj_root = tmp_path / "projects"
    proj_root.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", proj_root)
    monkeypatch.setattr(trash, "PROJECTS_DIR", proj_root)
    trash._LOCK_ATTEMPTS.clear()
    return proj_root


def _seed_marked(root: Path, name: str) -> Path:
    pdir = root / name
    pdir.mkdir()
    (pdir / "data.txt").write_text("x", encoding="utf-8")
    trash.mark_deleted(pdir)
    return pdir


def test_locked_entry_logs_once_across_sweeps(isolated_projects_root, monkeypatch, capsys):
    _seed_marked(isolated_projects_root, "locked-one")

    def _raise(path):
        raise PermissionError(f"[WinError 5] Access is denied: {path!r}")

    monkeypatch.setattr(shutil, "rmtree", _raise)

    trash.sweep_once()
    trash.sweep_once()
    trash.sweep_once()

    err = capsys.readouterr().err
    # First sweep logs the warning; subsequent sweeps must stay quiet.
    assert err.count("still locked") == 1, err


def test_clean_after_lock_logs_recovery(isolated_projects_root, monkeypatch, capsys):
    pdir = _seed_marked(isolated_projects_root, "recovers")

    # First call: simulate the lock. Second call: real rmtree.
    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def _flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("locked")
        return real_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", _flaky)

    trash.sweep_once()
    assert pdir.exists()
    cleaned = trash.sweep_once()

    assert cleaned == 1
    assert not pdir.exists()
    err = capsys.readouterr().err
    assert "still locked" in err
    assert "cleaned after retry" in err


def test_lock_attempts_clears_when_entry_disappears(isolated_projects_root, monkeypatch):
    """If the .deleted marker is removed externally, the sweeper stops
    seeing the entry and must drop it from _LOCK_ATTEMPTS so a future
    re-mark starts with a fresh log."""
    pdir = _seed_marked(isolated_projects_root, "vanishes")
    monkeypatch.setattr(
        shutil, "rmtree",
        lambda p: (_ for _ in ()).throw(PermissionError("x")),
    )
    trash.sweep_once()
    assert "vanishes" in trash._LOCK_ATTEMPTS

    # Marker gone → entry no longer sweep-eligible.
    (pdir / trash.MARKER_NAME).unlink()
    trash.sweep_once()
    assert "vanishes" not in trash._LOCK_ATTEMPTS
