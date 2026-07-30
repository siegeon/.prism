"""Durable provider-event store + signature verification (task c16cb8e3).

Signed webhook deliveries are recorded exactly once (delivery id is the primary
key, so a replay is a no-op) and persisted through the sqlite_db chokepoint so a
restart resumes pending work. A per-field source-of-truth policy records a
conflict when an inbound change targets a locally-owned field instead of
silently overwriting it.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from prism_service.services import sqlite_db


# ── signature verification ─────────────────────────────────────────────
def verify_hmac_sha256(secret: str, body: bytes, signature_header: str) -> bool:
    """Constant-time HMAC-SHA256 check of a ``sha256=<hex>`` header."""
    if not secret or not signature_header:
        return False
    header = signature_header.strip()
    if header.startswith("sha256="):
        header = header[len("sha256="):]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


# GitHub and Jira both sign with an HMAC-SHA256 shared secret over the raw body.
verify_github_signature = verify_hmac_sha256
verify_jira_signature = verify_hmac_sha256


@dataclass(frozen=True)
class IntegrationEvent:
    delivery_id: str
    workspace_id: str
    provider: str
    event_type: str
    payload_digest: str
    status: str
    received_at: str
    processed_at: str = ""
    note: str = ""


@dataclass(frozen=True)
class FieldConflict:
    id: int
    workspace_id: str
    entity_id: str
    field: str
    incoming_value: str
    recorded_at: str


class IntegrationEventStore:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS integration_events (
        delivery_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        event_type TEXT NOT NULL DEFAULT '',
        payload_digest TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        received_at TEXT NOT NULL,
        processed_at TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS field_conflicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        field TEXT NOT NULL,
        incoming_value TEXT NOT NULL DEFAULT '',
        recorded_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_events_status ON integration_events(status);
    CREATE INDEX IF NOT EXISTS idx_conflicts_ws ON field_conflicts(workspace_id);
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

    def record_delivery(
        self, workspace_id: str, provider: str, delivery_id: str,
        event_type: str = "", payload_digest: str = "",
    ) -> tuple[IntegrationEvent, bool]:
        """Persist a delivery. Returns (event, is_new). A duplicate delivery id
        is ignored (is_new False) so a replay applies exactly once."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO integration_events "
                "(delivery_id, workspace_id, provider, event_type, payload_digest, "
                "status, received_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (delivery_id, workspace_id, provider, event_type, payload_digest, now),
            )
            is_new = cur.rowcount == 1
        return self.get_event(delivery_id), is_new

    def get_event(self, delivery_id: str) -> Optional[IntegrationEvent]:
        row = self._db.execute(
            "SELECT * FROM integration_events WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        return self._event(row) if row else None

    def pending_events(self) -> list[IntegrationEvent]:
        rows = self._db.execute(
            "SELECT * FROM integration_events WHERE status = 'pending' "
            "ORDER BY received_at, delivery_id"
        ).fetchall()
        return [self._event(r) for r in rows]

    def mark_processed(self, delivery_id: str, note: str = "") -> None:
        with self._db:
            self._db.execute(
                "UPDATE integration_events SET status = 'processed', "
                "processed_at = ?, note = ? WHERE delivery_id = ?",
                (datetime.now(timezone.utc).isoformat(), note, delivery_id),
            )

    def apply_field_update(
        self, workspace_id: str, entity_id: str, field: str, incoming: str,
        owner: str,
    ) -> str:
        """Apply a provider-owned field, or RECORD a conflict for a
        locally-owned one. Returns 'applied' or 'conflict'. A locally-owned
        field is never silently overwritten."""
        if owner == "local":
            with self._db:
                self._db.execute(
                    "INSERT INTO field_conflicts "
                    "(workspace_id, entity_id, field, incoming_value, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (workspace_id, entity_id, field, incoming,
                     datetime.now(timezone.utc).isoformat()),
                )
            return "conflict"
        return "applied"

    def list_conflicts(self, workspace_id: str) -> list[FieldConflict]:
        rows = self._db.execute(
            "SELECT * FROM field_conflicts WHERE workspace_id = ? ORDER BY id",
            (workspace_id,),
        ).fetchall()
        return [self._conflict(r) for r in rows]

    @staticmethod
    def _event(row: sqlite3.Row) -> IntegrationEvent:
        return IntegrationEvent(
            delivery_id=row["delivery_id"], workspace_id=row["workspace_id"],
            provider=row["provider"], event_type=row["event_type"],
            payload_digest=row["payload_digest"], status=row["status"],
            received_at=row["received_at"], processed_at=row["processed_at"],
            note=row["note"],
        )

    @staticmethod
    def _conflict(row: sqlite3.Row) -> FieldConflict:
        return FieldConflict(
            id=row["id"], workspace_id=row["workspace_id"], entity_id=row["entity_id"],
            field=row["field"], incoming_value=row["incoming_value"],
            recorded_at=row["recorded_at"],
        )


_event_store: Optional[IntegrationEventStore] = None
_lock = threading.Lock()


def set_event_store(store: Optional[IntegrationEventStore]) -> None:
    global _event_store
    with _lock:
        _event_store = store


def get_event_store() -> IntegrationEventStore:
    global _event_store
    if _event_store is None:
        with _lock:
            if _event_store is None:
                from prism_service.data_dir import resolve_data_dir
                _event_store = IntegrationEventStore(
                    str(resolve_data_dir() / "integration_events.db"))
    return _event_store
