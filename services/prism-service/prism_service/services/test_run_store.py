"""Durable per-test run outcomes (task f3e8d477).

The evidence package must be able to answer "did the tests run, and did they
pass?". Before this, a run's statuses were computed by the tests endpoint and
returned — never stored — so the TESTS tab printed NOT RUN forever and no code
path could ever make a badge green.

This is the record. A run stamps each test's status, the TREE it ran against,
and when. The read path hydrates badges from it WITHOUT executing anything, so
page load stays cheap. Two honesty rules are baked in:

  * a record whose tree differs from the current tree is ``stale`` — evidence
    from another tree is never presented as the current result;
  * a test with NO record stays not-run — absence of evidence never reads as a
    pass.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from prism_service.services import sqlite_db


_SCHEMA = """
CREATE TABLE IF NOT EXISTS test_runs (
    task_id TEXT NOT NULL,
    test_name TEXT NOT NULL,
    file TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    tree_sha TEXT NOT NULL DEFAULT '',
    ran_at TEXT NOT NULL,
    PRIMARY KEY (task_id, test_name)
);
CREATE INDEX IF NOT EXISTS idx_test_runs_task ON test_runs(task_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestRunStore:
    """Owns test_runs.db. One connection per thread (never shared)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)
        self._tlocal = threading.local()
        self._db.executescript(_SCHEMA)

    @property
    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = sqlite_db.connect(self._db_path, timeout=5.0)
            self._tlocal.conn = conn
        return conn

    def record_run(self, task_id: str, tree_sha: str, rows) -> int:
        """Persist the outcome of a real run. ``rows`` are dicts carrying at
        least ``name`` and ``status``. The LAST run per test wins."""
        ran_at = _now()
        written = 0
        with self._db:
            for row in rows or []:
                name = str(row.get("name") or "").strip()
                status = str(row.get("status") or "").strip()
                if not name or not status:
                    continue
                self._db.execute(
                    "INSERT INTO test_runs "
                    "(task_id, test_name, file, status, tree_sha, ran_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (task_id, test_name) DO UPDATE SET "
                    "file=excluded.file, status=excluded.status, "
                    "tree_sha=excluded.tree_sha, ran_at=excluded.ran_at",
                    (task_id, name, str(row.get("file") or ""), status,
                     str(tree_sha or ""), ran_at),
                )
                written += 1
        return written

    def statuses_for(self, task_id: str) -> dict:
        rows = self._db.execute(
            "SELECT test_name, file, status, tree_sha, ran_at "
            "FROM test_runs WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        return {r["test_name"]: {"status": r["status"], "file": r["file"],
                                 "tree_sha": r["tree_sha"], "ran_at": r["ran_at"]}
                for r in rows}

    def close(self) -> None:
        conn = getattr(self._tlocal, "conn", None)
        if conn is not None:
            conn.close()
            del self._tlocal.conn


def hydrate(store: "TestRunStore", task_id: str, rows, current_tree: str):
    """Stamp discovered rows with the LAST REAL RUN.

    Never overwrites a status a caller already established (e.g. the gate
    receipt path). A row with no record is left alone — not-run stays not-run.
    """
    try:
        known = store.statuses_for(task_id)
    except Exception:
        return rows
    for row in rows or []:
        existing = row.get("status")
        if existing not in (None, "", "not-run"):
            continue  # already known (gate receipt) — do not clobber
        rec = known.get(str(row.get("name") or ""))
        if rec is None:
            continue  # absence of evidence is never a pass
        row["status"] = rec["status"]
        row["passed"] = rec["status"] == "passed"
        row["ran_at"] = rec["ran_at"]
        row["run_tree_sha"] = rec["tree_sha"]
        # Evidence from another tree is reported, but plainly marked stale.
        row["stale"] = bool(current_tree and rec["tree_sha"]
                            and rec["tree_sha"] != current_tree)
        row["verified_by"] = "recorded-run"
    return rows


_store: Optional[TestRunStore] = None
_lock = threading.Lock()


def set_test_run_store(store: Optional[TestRunStore]) -> None:
    global _store
    with _lock:
        _store = store


def get_test_run_store() -> TestRunStore:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                from prism_service.data_dir import resolve_data_dir
                _store = TestRunStore(str(resolve_data_dir() / "test_runs.db"))
    return _store
