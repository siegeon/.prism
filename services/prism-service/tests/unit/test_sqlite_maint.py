"""Unit tests — sqlite_maint WAL checkpointing (sqlite-hardening).

checkpoint_db must fold a populated -wal back into the main db file on
a short-lived connection, tolerate missing files, and never raise.
maint_interval_s must honor PRISM_SQLITE_MAINT_INTERVAL_S incl. 0=off.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import sqlite_maint as sm  # noqa: E402


def _make_wal_db(path: Path) -> Path:
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    conn.commit()
    # Keep the connection OPEN so the WAL is not auto-checkpointed on
    # close — mirroring the long-lived daemon readers that stranded a
    # week of tasks.db writes in the -wal live.
    return path


def test_checkpoint_db_truncates_wal(tmp_path):
    db = _make_wal_db(tmp_path / "store.db")
    wal = Path(str(db) + "-wal")
    assert wal.exists() and wal.stat().st_size > 0, "setup: WAL not populated"
    assert sm.checkpoint_db(db) is True
    assert wal.stat().st_size == 0, (
        "wal_checkpoint(TRUNCATE) must fold the -wal back into the db")


def test_checkpoint_db_missing_file_is_false_not_raise(tmp_path):
    assert sm.checkpoint_db(tmp_path / "nope.db") is False


def test_interval_env(monkeypatch):
    monkeypatch.delenv("PRISM_SQLITE_MAINT_INTERVAL_S", raising=False)
    assert sm.maint_interval_s() == float(sm.DEFAULT_INTERVAL_S)
    monkeypatch.setenv("PRISM_SQLITE_MAINT_INTERVAL_S", "60")
    assert sm.maint_interval_s() == 60.0
    monkeypatch.setenv("PRISM_SQLITE_MAINT_INTERVAL_S", "0")
    assert sm.maint_interval_s() == 0.0  # 0 = disabled
    monkeypatch.setenv("PRISM_SQLITE_MAINT_INTERVAL_S", "junk")
    assert sm.maint_interval_s() == float(sm.DEFAULT_INTERVAL_S)
