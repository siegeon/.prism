"""Issue #66 — crash/log capture is configured in-process so tracebacks
survive on every launch path, and the log rotates at startup (bounded
size, prior run preserved) instead of growing forever or being lost.
"""

from __future__ import annotations

import logging
import sys

import pytest

from prism_service import data_dir, main


def test_rotate_log_on_start_rolls_oversize_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(data_dir, "LOG_MAX_BYTES", 100)
    log = tmp_path / "prism.log"
    log.write_text("x" * 500, encoding="utf-8")
    data_dir.rotate_log_on_start(log)
    # The big current log became .1; a fresh run starts clean.
    assert (tmp_path / "prism.log.1").exists()
    assert (tmp_path / "prism.log.1").read_text(encoding="utf-8") == "x" * 500
    assert not log.exists()  # rolled away; the daemon reopens it in append mode


def test_rotate_log_on_start_keeps_small_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    log = tmp_path / "prism.log"
    log.write_text("small", encoding="utf-8")
    data_dir.rotate_log_on_start(log)
    assert log.read_text(encoding="utf-8") == "small"
    assert not (tmp_path / "prism.log.1").exists()


def test_configure_logging_installs_crash_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_logging_configured", False)
    saved_excepthook = sys.excepthook
    try:
        main._configure_logging()
        # An uncaught-exception hook is installed so a crash is LOGGED,
        # not just printed and lost (#66).
        assert sys.excepthook is not saved_excepthook
        assert logging.getLogger().level == logging.INFO
    finally:
        sys.excepthook = saved_excepthook


def test_configure_logging_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_logging_configured", False)
    saved_excepthook = sys.excepthook
    try:
        main._configure_logging()
        hook_after_first = sys.excepthook
        main._configure_logging()  # second call is a no-op
        assert sys.excepthook is hook_after_first
    finally:
        sys.excepthook = saved_excepthook


def test_does_not_reassign_stdio(tmp_path, monkeypatch):
    """Reassigning sys.stdout/stderr would break pytest capture and the
    interactive foreground console — the fd redirect handles routing."""
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_logging_configured", False)
    saved_excepthook = sys.excepthook
    out, err = sys.stdout, sys.stderr
    try:
        main._configure_logging()
        assert sys.stdout is out
        assert sys.stderr is err
    finally:
        sys.excepthook = saved_excepthook
