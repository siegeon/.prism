"""RED tests for the central sqlite connection chokepoint (task dde1162f).

trace: sqlite-hardening / connection-chokepoint. These FAIL until
``prism_service.services.sqlite_db.connect`` exists and applies the
canonical PRAGMAs (timeout=5.0, row_factory=Row, journal_mode=WAL,
busy_timeout=5000). Mirrors the v6.7.24 harness that measured a 97.6%
lock-error rate at 8 concurrent writers -> 0% after timeout+WAL.
"""
from __future__ import annotations

import sqlite3
import threading


def test_helper_contract(tmp_path):
    """AC-4: the helper applies Row + the three canonical PRAGMAs."""
    from prism_service.services import sqlite_db

    conn = sqlite_db.connect(tmp_path / "c.db")
    try:
        assert conn.row_factory is sqlite3.Row
        jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(jm).lower() == "wal"
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(bt) == 5000
    finally:
        conn.close()


def test_concurrent_writers_readers(tmp_path):
    """AC-1: N writers + M readers through the helper -> 0 lock/corruption
    errors. A bare sqlite3.connect() (no busy_timeout) reproduces the
    ~97.6% lock rate the v6.7.24 sweep fixed; the helper must be 0%."""
    from prism_service.services import sqlite_db

    db = tmp_path / "stress.db"
    with sqlite_db.connect(db) as c:
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v INTEGER)")
        c.commit()

    errors: list[str] = []
    barrier = threading.Barrier(16)

    def writer(n: int) -> None:
        barrier.wait()
        try:
            with sqlite_db.connect(db) as w:
                for i in range(25):
                    w.execute("INSERT INTO t(v) VALUES(?)", (n * 1000 + i,))
                w.commit()
        except sqlite3.Error as exc:  # lock/corruption surfaces here
            errors.append(f"writer {n}: {exc!r}")

    def reader() -> None:
        barrier.wait()
        try:
            with sqlite_db.connect(db) as r:
                for _ in range(25):
                    r.execute("SELECT COUNT(*) FROM t").fetchone()
        except sqlite3.Error as exc:
            errors.append(f"reader: {exc!r}")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    threads += [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"{len(errors)}/16 workers hit lock/corruption: {errors[:3]}"
    with sqlite_db.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 8 * 25
