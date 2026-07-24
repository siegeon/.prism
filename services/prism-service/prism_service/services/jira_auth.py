"""Workspace/cloudId-scoped Atlassian OAuth token store (task fbe9f26c).

Corrects the stale branch's single global data-dir credential: tokens are now
scoped to (workspace_id, cloud_id) and stored durably through the sqlite_db
chokepoint. Access-token expiry is handled here — ``access_token()`` refreshes
transparently on expiry and re-persists the new pair. No method ever returns a
raw token; only a masked ``•••<last4>`` fingerprint is exposed.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from prism_service.services import sqlite_db


class JiraAuthError(RuntimeError):
    """Raised when a (workspace, cloudId) connection is missing."""


class JiraAuthStore:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS jira_connections (
        workspace_id TEXT NOT NULL,
        cloud_id TEXT NOT NULL,
        access_token TEXT NOT NULL DEFAULT '',
        refresh_token TEXT NOT NULL DEFAULT '',
        expires_at INTEGER NOT NULL DEFAULT 0,
        site_url TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (workspace_id, cloud_id)
    );
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)
        self._tlocal = threading.local()
        self._db.executescript(self._SCHEMA)

    @property
    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = sqlite_db.connect(self._db_path, timeout=5.0)
            self._tlocal.conn = conn
        return conn

    def set_connection(
        self, workspace_id: str, cloud_id: str, access_token: str,
        refresh_token: str, expires_at: int, site_url: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._db:
            self._db.execute(
                "INSERT INTO jira_connections "
                "(workspace_id, cloud_id, access_token, refresh_token, expires_at, "
                "site_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (workspace_id, cloud_id) DO UPDATE SET "
                "access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
                "expires_at=excluded.expires_at, site_url=excluded.site_url, "
                "updated_at=excluded.updated_at",
                (workspace_id, cloud_id, access_token, refresh_token, int(expires_at),
                 site_url, now, now),
            )

    def _row(self, workspace_id: str, cloud_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM jira_connections WHERE workspace_id = ? AND cloud_id = ?",
            (workspace_id, cloud_id),
        ).fetchone()
        if row is None:
            raise JiraAuthError(f"no jira connection for {workspace_id}/{cloud_id}")
        return row

    def access_token(
        self, workspace_id: str, cloud_id: str, now: int,
        refresh: Callable[[str], dict],
    ) -> str:
        """A currently-valid access token, refreshing on expiry.

        ``now`` is epoch seconds (injected so tests are deterministic).
        ``refresh(refresh_token) -> {access_token, refresh_token?, expires_in}``
        is called only when the stored token has expired; the fresh pair is
        persisted before the new access token is returned.
        """
        row = self._row(workspace_id, cloud_id)
        if int(now) < int(row["expires_at"]):
            return row["access_token"]
        result = refresh(row["refresh_token"])
        new_access = result["access_token"]
        new_refresh = result.get("refresh_token") or row["refresh_token"]
        expires_at = int(now) + int(result.get("expires_in", 3600))
        self.set_connection(workspace_id, cloud_id, new_access, new_refresh,
                            expires_at, site_url=row["site_url"])
        return new_access

    def fingerprint(self, workspace_id: str, cloud_id: str) -> str:
        row = self._row(workspace_id, cloud_id)
        token = row["access_token"] or ""
        tail = token[-4:] if len(token) >= 4 else token
        return f"•••{tail}"


_store: Optional[JiraAuthStore] = None
_lock = threading.Lock()


def set_jira_auth_store(store: Optional[JiraAuthStore]) -> None:
    global _store
    with _lock:
        _store = store


def get_jira_auth_store() -> JiraAuthStore:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                from prism_service.data_dir import resolve_data_dir
                _store = JiraAuthStore(str(resolve_data_dir() / "jira_auth.db"))
    return _store
