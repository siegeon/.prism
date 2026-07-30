"""Whether the user wants PRISM to sync with a provider.

Owner correction 2026-07-28: "i did not enable this to sync with github, you
just appear to have assumed that."

Connecting a provider proves a credential works. It says NOTHING about whether
you want your work synced there. Those were the same thing only because no
value existed anywhere that could say "off" — which is exactly why the
assumption was invisible.

The default is OFF and it is expressed as the ABSENCE of a row, not as a stored
False. A default that is written down can drift; a default that is "nobody has
said yes" cannot.

Keyed by (scope, provider), so GitHub on and Jira off is expressible
(mx-639efa: providers are optional and PRISM's own tasks are the work of
record).
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from prism_service.services import sqlite_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_prefs (
    scope     TEXT NOT NULL,
    provider  TEXT NOT NULL,
    enabled   INTEGER NOT NULL,
    PRIMARY KEY (scope, provider)
)
"""


class SyncPreferences:
    """Per-scope, per-provider sync consent."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._local = threading.local()
        with self._conn() as conn:
            conn.execute(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # Route through the helper with an explicit timeout: a bare
            # connect() fails "database is locked" under concurrent writers
            # (measured 97.6% error rate at 8 writers, 0.0% with timeout=5.0),
            # and the sync switch is written from the SPA while the sync worker
            # reads it.
            conn = sqlite_db.connect(self._path, check_same_thread=False,
                                     timeout=5.0)
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def enabled(self, scope: str, provider: str) -> bool:
        """False unless someone explicitly turned it on."""
        row = self._conn().execute(
            "SELECT enabled FROM sync_prefs WHERE scope=? AND provider=?",
            (scope, provider)).fetchone()
        return bool(row[0]) if row else False

    def set_enabled(self, scope: str, provider: str, enabled: bool) -> bool:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sync_prefs (scope, provider, enabled) "
                "VALUES (?, ?, ?) ON CONFLICT(scope, provider) "
                "DO UPDATE SET enabled=excluded.enabled",
                (scope, provider, 1 if enabled else 0))
        return bool(enabled)


_preferences: Optional[SyncPreferences] = None


def set_sync_preferences(prefs: Optional[SyncPreferences]) -> None:
    global _preferences
    _preferences = prefs


def get_sync_preferences() -> SyncPreferences:
    global _preferences
    if _preferences is None:
        from prism_service.data_dir import resolve_data_dir
        _preferences = SyncPreferences(str(resolve_data_dir() / "sync_prefs.db"))
    return _preferences
