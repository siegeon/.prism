"""Opt-in, loop-safe outbound outbox (task c16cb8e3).

Outbound sync is DISABLED by default and enabled per (workspace, connection).
Enqueued items and the enablement policy are durable through the sqlite_db
chokepoint so a restart resumes them. A sent item records the delivery marker it
produced; an inbound event carrying that marker is recognized as our own echo
and suppressed, so an enabled outbound change can never form an update loop.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from prism_service.services import sqlite_db


@dataclass(frozen=True)
class OutboxItem:
    id: str
    workspace_id: str
    connection_id: str
    entity_id: str
    field: str
    value: str
    status: str
    marker: str
    created_at: str
    sent_at: str = ""


class IntegrationOutbox:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS outbound_policy (
        workspace_id TEXT NOT NULL,
        connection_id TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (workspace_id, connection_id)
    );
    CREATE TABLE IF NOT EXISTS outbox_items (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        connection_id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        field TEXT NOT NULL,
        value TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        marker TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        sent_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_items(status);
    CREATE INDEX IF NOT EXISTS idx_outbox_marker ON outbox_items(workspace_id, marker);
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

    # ── policy ────────────────────────────────────────────────────────
    def enable_outbound(self, workspace_id: str, connection_id: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO outbound_policy (workspace_id, connection_id, enabled, updated_at) "
                "VALUES (?, ?, 1, ?) ON CONFLICT (workspace_id, connection_id) "
                "DO UPDATE SET enabled = 1, updated_at = excluded.updated_at",
                (workspace_id, connection_id, datetime.now(timezone.utc).isoformat()),
            )

    def is_enabled(self, workspace_id: str, connection_id: str) -> bool:
        row = self._db.execute(
            "SELECT enabled FROM outbound_policy WHERE workspace_id = ? AND connection_id = ?",
            (workspace_id, connection_id),
        ).fetchone()
        return bool(row["enabled"]) if row else False

    # ── outbox ────────────────────────────────────────────────────────
    def enqueue(
        self, workspace_id: str, connection_id: str, entity_id: str, field: str,
        value: str, origin_marker: str = "",
    ) -> Optional[OutboxItem]:
        """Queue an outbound change — but ONLY when outbound is enabled for this
        connection AND the change is not an echo of one we already sent.
        Returns the item, or None when refused."""
        if not self.is_enabled(workspace_id, connection_id):
            return None
        if origin_marker and self.is_echo(workspace_id, origin_marker):
            return None
        item_id = str(uuid4())
        with self._db:
            self._db.execute(
                "INSERT INTO outbox_items "
                "(id, workspace_id, connection_id, entity_id, field, value, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (item_id, workspace_id, connection_id, entity_id, field, value,
                 datetime.now(timezone.utc).isoformat()),
            )
        return self.get_item(item_id)

    def get_item(self, item_id: str) -> Optional[OutboxItem]:
        row = self._db.execute(
            "SELECT * FROM outbox_items WHERE id = ?", (item_id,)
        ).fetchone()
        return self._item(row) if row else None

    def pending_items(self, workspace_id: Optional[str] = None) -> list[OutboxItem]:
        sql = "SELECT * FROM outbox_items WHERE status = 'pending'"
        params: list[str] = []
        if workspace_id is not None:
            sql += " AND workspace_id = ?"
            params.append(workspace_id)
        sql += " ORDER BY created_at, id"
        return [self._item(r) for r in self._db.execute(sql, params).fetchall()]

    def mark_sent(self, item_id: str, marker: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE outbox_items SET status = 'sent', marker = ?, sent_at = ? "
                "WHERE id = ?",
                (marker, datetime.now(timezone.utc).isoformat(), item_id),
            )

    def is_echo(self, workspace_id: str, marker: str) -> bool:
        """True when an inbound marker matches an item WE sent — the change is
        our own echo and must not be re-enqueued (loop prevention)."""
        if not marker:
            return False
        row = self._db.execute(
            "SELECT 1 FROM outbox_items WHERE workspace_id = ? AND marker = ? "
            "AND status = 'sent' LIMIT 1",
            (workspace_id, marker),
        ).fetchone()
        return row is not None

    @staticmethod
    def _item(row: sqlite3.Row) -> OutboxItem:
        return OutboxItem(
            id=row["id"], workspace_id=row["workspace_id"],
            connection_id=row["connection_id"], entity_id=row["entity_id"],
            field=row["field"], value=row["value"], status=row["status"],
            marker=row["marker"], created_at=row["created_at"], sent_at=row["sent_at"],
        )


_outbox: Optional[IntegrationOutbox] = None
_lock = threading.Lock()


def set_outbox(outbox: Optional[IntegrationOutbox]) -> None:
    global _outbox
    with _lock:
        _outbox = outbox


def get_outbox() -> IntegrationOutbox:
    global _outbox
    if _outbox is None:
        with _lock:
            if _outbox is None:
                from prism_service.data_dir import resolve_data_dir
                _outbox = IntegrationOutbox(str(resolve_data_dir() / "integration_outbox.db"))
    return _outbox
