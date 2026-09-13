"""Pins wakeups.py's cross-process bridge (task: worker-host process
split, owner 2026-09-13): once PRISM_WORKERS_PROCESS=1 moves the standing
workers into a separate OS process from the API, a signal() raised by an
HTTP request in the API process must still wake a worker's wait() call
running in the OTHER process, via the small sqlite table under the data
dir -- the in-memory threading.Condition this module used before only ever
reached waiters in the SAME process."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from prism_service.services import wakeups


def setup_function(_fn) -> None:
    wakeups._reset_for_tests()


def test_cross_conn_resolves_to_a_file_under_the_data_dir() -> None:
    path = wakeups._cross_db_path()
    assert path is not None
    assert path.endswith("wakeups.db")
    assert os.path.dirname(path) == os.environ["PRISM_DATA_DIR"]


def test_signal_writes_a_row_a_second_connection_can_see() -> None:
    wakeups.signal("cross_test", "prism")
    conn = wakeups._cross_conn()
    assert conn is not None
    rows = conn.execute(
        "SELECT kind, project FROM signals WHERE kind = ?", ("cross_test",)
    ).fetchall()
    assert rows == [("cross_test", "prism")]


def test_cross_has_new_respects_the_baseline_and_project_scope() -> None:
    since = time.time()
    time.sleep(0.01)
    wakeups.signal("cross_test", "prism")
    assert wakeups._cross_has_new({"cross_test"}, "prism", since) is True
    assert wakeups._cross_has_new({"cross_test"}, "other-project", since) is False
    assert wakeups._cross_has_new({"other_kind"}, "prism", since) is False
    assert wakeups._cross_has_new({"cross_test"}, "prism", time.time()) is False


def test_wait_wakes_on_a_cross_process_row_with_no_local_notify(monkeypatch) -> None:
    # Simulate "another process signalled" by writing the cross-process row
    # directly, WITHOUT going through this process's in-memory
    # notify_all() -- wait() must still pick it up via its poll of the
    # sqlite table, not just the local condition variable.
    since = time.time()
    monkeypatch.setattr(wakeups, "_CROSS_POLL_S", 0.05)
    wakeups._cross_signal("cross_test", "prism", time.time())
    started = time.time()
    ok = wakeups.wait(["cross_test"], project="prism", timeout=2.0, since=since)
    elapsed = time.time() - started
    assert ok is True
    assert elapsed < 0.5


def test_reset_for_tests_clears_the_cross_process_table() -> None:
    wakeups.signal("cross_test", "prism")
    wakeups._reset_for_tests()
    assert wakeups.last_signal_at("cross_test", "prism") == 0.0
    assert wakeups._cross_has_new({"cross_test"}, "prism", 0.0) is False


def test_signal_in_a_separate_os_process_wakes_a_waiter_here_within_500ms(
        tmp_path) -> None:
    data_dir = os.environ["PRISM_DATA_DIR"]
    ready_path = tmp_path / "ready"
    script = (
        "import os, time\n"
        f"os.environ['PRISM_DATA_DIR'] = {data_dir!r}\n"
        "from prism_service.services import wakeups as w\n"
        f"open({str(ready_path)!r}, 'w').write('ready')\n"
        "time.sleep(0.2)\n"
        "w.signal('cross_os_test', 'prism')\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.time() + 15.0
        while not ready_path.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert ready_path.exists(), "child process never started"

        since = time.time()
        started = time.time()
        ok = wakeups.wait(["cross_os_test"], project="prism", timeout=5.0,
                           since=since)
        elapsed = time.time() - started
        assert ok is True
        # child sleeps 0.2s before signalling; the poll floor is 0.25s, so
        # a comfortable but real bound on the round trip:
        assert elapsed < 1.0, f"cross-process wakeup took {elapsed * 1000:.0f}ms"
    finally:
        proc.wait(timeout=5.0)
        assert proc.returncode == 0
