"""RED test for auto-discovered sqlite maintenance (task dde1162f, AC-2).

trace: sqlite-hardening / maint-glob-discovery. sqlite_maint currently
walks a hardcoded 5-tuple (_STORE_RELPATHS); a db added under a NEW
subdir bloats its -wal forever. This FAILS until a recursive
glob-discovery entry point (checkpoint_data_dir) exists and folds the
-wal of a db it was never explicitly told about.
"""
from __future__ import annotations

import sqlite3


def _open_wal_db(path):
    """Open a WAL db and leave an uncheckpointed -wal sidecar behind.

    Returns the OPEN connection: the -wal only persists while a
    connection is open (the last close auto-checkpoints + removes it),
    and wal_autocheckpoint=0 stops SQLite folding it mid-write."""
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    for i in range(200):
        conn.execute("INSERT INTO t(v) VALUES(?)", (f"row-{i}" * 20,))
    conn.commit()
    return conn


def test_checkpoints_new_subdir_db(tmp_path):
    from prism_service.services import sqlite_maint

    # A db under a subdir the hardcoded tuple never mentions.
    sub = tmp_path / "never_told" / "deep"
    sub.mkdir(parents=True)
    db = sub / "fresh.db"
    conn = _open_wal_db(db)
    try:
        wal = db.with_name(db.name + "-wal")
        assert wal.exists() and wal.stat().st_size > 0, "precondition: -wal present"

        # Discover + checkpoint every *.db under the data dir by glob.
        n = sqlite_maint.checkpoint_data_dir(tmp_path)

        assert n >= 1, "glob discovery checkpointed nothing under the data dir"
        # TRUNCATE checkpoint folds the -wal back: it is gone or empty.
        assert wal.stat().st_size == 0, (
            "fresh-subdir db was not checkpointed — glob discovery missed it")
    finally:
        conn.close()
