"""Jira project-mapping store — bind a PRISM project to a Jira project key.

Clones the UserService / TaskService store shape (per-thread sqlite handle,
idempotent schema create, DATA_DIR-level so it spans projects like users.db).
Replaces the single global PRISM_JIRA_PROJECT_KEY env with a real per-project
mapping: outbound sync resolves a task's Jira project from here FIRST, and
only falls back to the env when a project has no mapping.

Layering: this is a SERVICE — it imports data_dir only, never api/ or models.
Never stores a token — only the Jira project KEY (e.g. "PLAT").
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from prism_service.data_dir import resolve_data_dir


_CREATE_MAP_SQL = """
CREATE TABLE IF NOT EXISTS jira_project_map (
    project_id TEXT PRIMARY KEY,
    jira_project_key TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


class JiraMappingStore:
    """Manages the jira_mappings.db lifecycle: get/set/list/delete."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        # DATA_DIR-level, cross-project (mirrors UserService): a mapping is
        # keyed by PRISM project_id, orthogonal to any one project's store.
        # Callers may pass an explicit path (tests / isolation); default is
        # resolve_data_dir()/jira_mappings.db.
        self._db_path = db_path or str(resolve_data_dir() / "jira_mappings.db")
        self._tlocal = threading.local()
        self._db.executescript(_CREATE_MAP_SQL)

    @property
    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._tlocal.conn = conn
        return conn

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, project_id: str) -> str:
        """The Jira project key bound to `project_id`, or '' when unmapped."""
        row = self._db.execute(
            "SELECT jira_project_key FROM jira_project_map WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return (row["jira_project_key"] if row else "") or ""

    def set(self, project_id: str, jira_project_key: str) -> dict:
        """Upsert the mapping (project_id -> jira_project_key). Returns the row."""
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "INSERT INTO jira_project_map (project_id, jira_project_key, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET "
            "jira_project_key=excluded.jira_project_key, updated_at=excluded.updated_at",
            (project_id, jira_project_key, now),
        )
        self._db.commit()
        return {"project_id": project_id, "jira_project_key": jira_project_key,
                "updated_at": now}

    def list(self) -> list[dict]:
        """All mappings, newest-updated first: [{project_id, jira_project_key,
        updated_at}]."""
        rows = self._db.execute(
            "SELECT project_id, jira_project_key, updated_at "
            "FROM jira_project_map ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, project_id: str) -> bool:
        """Remove a mapping. Returns True when a row was deleted. Idempotent."""
        cur = self._db.execute(
            "DELETE FROM jira_project_map WHERE project_id = ?", (project_id,)
        )
        self._db.commit()
        return cur.rowcount > 0


# ----------------------------------------------------------------------
# Process-wide default store (lazy, once) — mirrors users.default_service
# ----------------------------------------------------------------------

_DEFAULT_SERVICE: Optional[JiraMappingStore] = None
_DEFAULT_LOCK = threading.Lock()


def default_service() -> JiraMappingStore:
    """The DATA_DIR-level JiraMappingStore the live process shares."""
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_SERVICE is None:
                _DEFAULT_SERVICE = JiraMappingStore()
    return _DEFAULT_SERVICE
