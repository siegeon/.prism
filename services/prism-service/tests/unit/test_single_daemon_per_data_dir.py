"""A stray daemon can never shadow the dev instance (task 033cb54a).

On 2026-08-16 a second prism_service.main (leaked by a test run onto
fallback ports) ran against the REAL data dir for 18 hours and read 583GB
from its SQLite files — every UI query competed with that firehose. The
fix is an OS-level exclusive lock on <data_dir>/daemon.lock, acquired
before the pidfile or any port bind: a second launch against the same
data dir refuses with a loud exit instead of silently double-running.
The lock is released by the OS when the holder dies, so there is no
stale-lock analogue of the stale-pidfile problem.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _acquire_in_subprocess(data_dir: Path) -> "subprocess.Popen":
    """Hold the lock from a REAL second process (file locks are per-process,
    so an in-process second acquire would not exercise the OS semantics)."""
    code = textwrap.dedent(f"""
        import os, sys, time
        os.environ["PRISM_DATA_DIR"] = {str(data_dir)!r}
        sys.path.insert(0, {str(_SERVICE_ROOT)!r})
        from prism_service import main as m
        got = m._acquire_single_daemon_lock()
        print("HELD" if got is not None else "REFUSED", flush=True)
        if got is None:
            sys.exit(2)
        time.sleep(30)
    """)
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_second_acquire_refuses_while_first_lives(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PRISM_ALLOW_MULTI_DAEMON", raising=False)
    holder = _acquire_in_subprocess(tmp_path)
    try:
        assert holder.stdout.readline().strip() == "HELD"
        from prism_service import main as m
        assert m._acquire_single_daemon_lock() is None, (
            "a second daemon acquired the same data dir's lock — the "
            "exact double-run that shadowed the store for 18h")
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_lock_frees_when_holder_dies(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PRISM_ALLOW_MULTI_DAEMON", raising=False)
    holder = _acquire_in_subprocess(tmp_path)
    assert holder.stdout.readline().strip() == "HELD"
    holder.kill()
    holder.wait(timeout=10)
    from prism_service import main as m
    got = m._acquire_single_daemon_lock()
    assert got is not None, (
        "the lock outlived its dead holder — a crashed daemon would "
        "block every restart")
    # Release for later tests in this process.
    try:
        got.close()
        m._DAEMON_LOCK_FH = None
    except Exception:
        pass


def test_opt_out_env_skips_the_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRISM_ALLOW_MULTI_DAEMON", "1")
    from prism_service import main as m
    assert m._acquire_single_daemon_lock() is True
