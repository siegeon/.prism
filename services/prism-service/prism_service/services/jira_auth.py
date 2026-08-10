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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from prism_service.services import sqlite_db


class JiraAuthError(RuntimeError):
    """Raised when a (workspace, cloudId) connection is missing."""


@dataclass(frozen=True)
class JiraCredential:
    """A resolved Jira credential (task 64ba4755, FR-3).

    ``kind`` is 'oauth' or 'api_token'. Only 'api_token' carries an email
    (Basic auth needs it); 'oauth' keeps using a bare bearer-token string
    everywhere else, so this type only ever appears for the api_token path.
    """

    kind: str
    secret: str
    email: str = ""
    site_url: str = ""
    cloud_id: str = ""


class JiraAuthStore:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS jira_connections (
        workspace_id TEXT NOT NULL,
        cloud_id TEXT NOT NULL,
        access_token TEXT NOT NULL DEFAULT '',
        refresh_token TEXT NOT NULL DEFAULT '',
        expires_at INTEGER NOT NULL DEFAULT 0,
        site_url TEXT NOT NULL DEFAULT '',
        auth_kind TEXT NOT NULL DEFAULT 'oauth',
        account_email TEXT NOT NULL DEFAULT '',
        account_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (workspace_id, cloud_id)
    );
    """

    # Additive migration (task 64ba4755, FR-2): a pre-existing OAuth row
    # written before these columns existed must upgrade IN PLACE, never via
    # a table recreate — auth_kind defaults to 'oauth' so it keeps reading
    # exactly as it did (test_connectors_section.py's expired-credential case).
    _ADDITIVE_COLUMNS = (
        ("auth_kind", "TEXT NOT NULL DEFAULT 'oauth'"),
        ("account_email", "TEXT NOT NULL DEFAULT ''"),
        ("account_name", "TEXT NOT NULL DEFAULT ''"),
    )

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)
        self._tlocal = threading.local()
        self._db.executescript(self._SCHEMA)
        self._migrate_additive()

    def _migrate_additive(self) -> None:
        existing = {row[1] for row in
                    self._db.execute("PRAGMA table_info(jira_connections)").fetchall()}
        for name, ddl in self._ADDITIVE_COLUMNS:
            if name not in existing:
                with self._db:
                    self._db.execute(
                        f"ALTER TABLE jira_connections ADD COLUMN {name} {ddl}")

    @property
    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = sqlite_db.connect(self._db_path, timeout=5.0)
            self._tlocal.conn = conn
        return conn

    @property
    def _fernet(self):
        """Per-instance key-encryption key, in a file beside the db (same
        pattern as workspace_service._fernet) — a db dump or backup leaks
        ciphertext, never the raw access/refresh token (task 64ba4755 NFR)."""
        f = getattr(self, "_fernet_cache", None)
        if f is not None:
            return f
        from cryptography.fernet import Fernet
        key_path = Path(self._db_path).with_name(".jira_token_key")
        if key_path.exists():
            key = key_path.read_bytes()
        else:
            key = Fernet.generate_key()
            key_path.write_bytes(key)
            try:
                import os
                os.chmod(key_path, 0o600)  # best-effort; a no-op on Windows
            except OSError:
                pass
        f = Fernet(key)
        self._fernet_cache = f
        return f

    def _encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, stored: str) -> str:
        """Plaintext for a stored value. A pre-encryption plaintext row
        (written directly, before this store ever encrypted anything —
        e.g. a legacy OAuth row) fails Fernet's own format check and is
        returned as-is rather than raised on."""
        if not stored:
            return ""
        try:
            from cryptography.fernet import InvalidToken
            try:
                return self._fernet.decrypt(stored.encode("ascii")).decode("utf-8")
            except InvalidToken:
                return stored
        except Exception:
            return stored

    def set_connection(
        self, workspace_id: str, cloud_id: str, access_token: str,
        refresh_token: str, expires_at: int, site_url: str = "",
        auth_kind: str = "oauth", account_email: str = "", account_name: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._db:
            self._db.execute(
                "INSERT INTO jira_connections "
                "(workspace_id, cloud_id, access_token, refresh_token, expires_at, "
                "site_url, auth_kind, account_email, account_name, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (workspace_id, cloud_id) DO UPDATE SET "
                "access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
                "expires_at=excluded.expires_at, site_url=excluded.site_url, "
                "auth_kind=excluded.auth_kind, account_email=excluded.account_email, "
                "account_name=excluded.account_name, updated_at=excluded.updated_at",
                (workspace_id, cloud_id, self._encrypt(access_token),
                 self._encrypt(refresh_token), int(expires_at),
                 site_url, auth_kind, account_email, account_name, now, now),
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

        An ``api_token`` row (task 64ba4755, FR-2) has no refresh grant at
        all — it never expires by this store's clock, so it returns the
        stored secret directly rather than looping into the OAuth refresh
        path, which would read a healthy credential as needs_attention
        forever.
        """
        row = self._row(workspace_id, cloud_id)
        if (row["auth_kind"] or "oauth") == "api_token":
            return self._decrypt(row["access_token"])
        if int(now) < int(row["expires_at"]):
            return self._decrypt(row["access_token"])
        result = refresh(self._decrypt(row["refresh_token"]))
        new_access = result["access_token"]
        new_refresh = result.get("refresh_token") or self._decrypt(row["refresh_token"])
        expires_at = int(now) + int(result.get("expires_in", 3600))
        self.set_connection(workspace_id, cloud_id, new_access, new_refresh,
                            expires_at, site_url=row["site_url"])
        return new_access

    def fingerprint(self, workspace_id: str, cloud_id: str) -> str:
        row = self._row(workspace_id, cloud_id)
        token = self._decrypt(row["access_token"] or "")
        tail = token[-4:] if len(token) >= 4 else token
        return f"•••{tail}"

    def auth_kind(self, workspace_id: str, cloud_id: str) -> str:
        return self._row(workspace_id, cloud_id)["auth_kind"] or "oauth"

    def site_url(self, workspace_id: str, cloud_id: str) -> str:
        try:
            row = self._row(workspace_id, cloud_id)
        except JiraAuthError:
            return ""
        return row["site_url"] or ""

    def credential(self, workspace_id: str, cloud_id: str) -> JiraCredential:
        """The api_token credential for a connection (task 64ba4755, FR-3).

        Only meaningful for an ``api_token`` row; an oauth row's bearer
        token stays a bare string everywhere else, resolved via
        ``access_token()`` instead.
        """
        row = self._row(workspace_id, cloud_id)
        return JiraCredential(
            kind=row["auth_kind"] or "oauth",
            secret=self._decrypt(row["access_token"] or ""),
            email=row["account_email"] or "", site_url=row["site_url"] or "",
            cloud_id=cloud_id,
        )


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
