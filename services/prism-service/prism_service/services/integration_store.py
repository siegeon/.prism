"""Workspace-scoped integration substrate store (task fddfd75a).

One global ``integrations.db`` beside ``workspace.db``, opened only through the
``sqlite_db.connect()`` chokepoint with ``PRAGMA foreign_keys=ON`` and a
per-thread connection (mirrors WorkspaceService/TaskService). EVERY public
method requires ``workspace_id``; there is no unscoped id lookup. Composite
foreign keys ``(child, workspace_id) -> parent(id, workspace_id)`` make it
impossible to pair an id from workspace A with a row in workspace B.

No token, secret, credential, or raw provider payload column exists here — the
substrate stores identity + display + sanitized audit rows only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from prism_service.data_dir import resolve_data_dir
from prism_service.services import sqlite_db
from prism_service.models.integration import (
    ExternalContainer,
    ExternalEntity,
    ExternalEntityInput,
    ExternalMessage,
    ExternalMessageInput,
    IntegrationConnection,
    SyncReceipt,
    SyncRun,
    WorkItemExternalLink,
    connection_id_for,
    container_id_for,
    entity_id_for,
    link_id_for,
    message_id_for,
    normalize_status_category,
)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS integration_connections (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    remote_scope TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, provider, remote_scope),
    UNIQUE (id, workspace_id)
);

CREATE TABLE IF NOT EXISTS external_containers (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    display_key TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, connection_id, kind, remote_id),
    UNIQUE (id, workspace_id),
    FOREIGN KEY (connection_id, workspace_id)
        REFERENCES integration_connections (id, workspace_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS external_entities (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    container_id TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    display_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    remote_status TEXT NOT NULL DEFAULT '',
    status_category TEXT NOT NULL DEFAULT '',
    assignees TEXT NOT NULL DEFAULT '[]',
    revision TEXT NOT NULL DEFAULT '',
    remote_updated_at TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (id, workspace_id),
    FOREIGN KEY (container_id, workspace_id)
        REFERENCES external_containers (id, workspace_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS external_messages (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    remote_created_at TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, connection_id, provider, remote_id),
    UNIQUE (id, workspace_id),
    FOREIGN KEY (connection_id, workspace_id)
        REFERENCES integration_connections (id, workspace_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS work_item_external_links (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    link_type TEXT NOT NULL DEFAULT 'import',
    state TEXT NOT NULL DEFAULT 'provisioning'
        CHECK (state IN ('provisioning', 'active')),
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, entity_id, link_type),
    FOREIGN KEY (entity_id, workspace_id)
        REFERENCES external_entities (id, workspace_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_cursors (
    workspace_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    container_id TEXT NOT NULL,
    stream TEXT NOT NULL DEFAULT 'default',
    cursor TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, connection_id, container_id, stream)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    container_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    error_code TEXT NOT NULL DEFAULT '',
    items_processed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_receipts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_containers_ws_conn
    ON external_containers (workspace_id, connection_id);
CREATE INDEX IF NOT EXISTS idx_entities_ws_container
    ON external_entities (workspace_id, container_id);
CREATE INDEX IF NOT EXISTS idx_messages_ws_connection
    ON external_messages (workspace_id, connection_id);
CREATE INDEX IF NOT EXISTS idx_links_ws_task
    ON work_item_external_links (workspace_id, task_id);
CREATE INDEX IF NOT EXISTS idx_runs_ws_container
    ON sync_runs (workspace_id, container_id);
CREATE INDEX IF NOT EXISTS idx_receipts_ws_run
    ON sync_receipts (workspace_id, run_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(value: str, field: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


class IntegrationStore:
    """Owns integrations.db. Connections are per-thread (never shared)."""

    def __init__(self, db_path: "str | Path") -> None:
        self._db_path = str(db_path)
        self._tlocal = threading.local()
        self._db.executescript(_SCHEMA_SQL)

    @property
    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = sqlite_db.connect(self._db_path, timeout=5.0)
            conn.execute("PRAGMA foreign_keys=ON")
            self._tlocal.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._tlocal, "conn", None)
        if conn is not None:
            conn.close()
            del self._tlocal.conn

    # ── connections ───────────────────────────────────────────────────
    def ensure_connection(
        self, workspace_id: str, provider: str, remote_scope: str,
        display_name: str = "",
    ) -> IntegrationConnection:
        ws = _require(workspace_id, "workspace_id")
        provider = _require(provider, "provider")
        remote_scope = _require(remote_scope, "remote_scope")
        cid = connection_id_for(ws, provider, remote_scope)
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO integration_connections "
                "(id, workspace_id, provider, remote_scope, display_name, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cid, ws, provider, remote_scope, display_name.strip(), _now()),
            )
        got = self.get_connection(ws, cid)
        if got is None:  # pragma: no cover - defensive
            raise RuntimeError("connection write did not persist")
        return got

    def get_connection(self, workspace_id: str, connection_id: str) -> Optional[IntegrationConnection]:
        row = self._db.execute(
            "SELECT * FROM integration_connections WHERE id = ? AND workspace_id = ?",
            (connection_id, workspace_id),
        ).fetchone()
        return self._connection(row) if row else None

    def list_connections(self, workspace_id: str, provider: Optional[str] = None) -> list[IntegrationConnection]:
        sql = "SELECT * FROM integration_connections WHERE workspace_id = ?"
        params: list[str] = [workspace_id]
        if provider is not None:
            sql += " AND provider = ?"
            params.append(provider)
        sql += " ORDER BY created_at, id"
        return [self._connection(r) for r in self._db.execute(sql, params).fetchall()]

    # ── containers ────────────────────────────────────────────────────
    def ensure_container(
        self, workspace_id: str, connection_id: str, kind: str, remote_id: str,
        display_key: str = "", display_name: str = "", url: str = "",
    ) -> ExternalContainer:
        ws = _require(workspace_id, "workspace_id")
        connection_id = _require(connection_id, "connection_id")
        kind = _require(kind, "kind")
        remote_id = _require(remote_id, "remote_id")
        if self.get_connection(ws, connection_id) is None:
            raise ValueError("connection does not belong to this workspace")
        kid = container_id_for(ws, connection_id, kind, remote_id)
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO external_containers "
                "(id, workspace_id, connection_id, kind, remote_id, display_key, "
                "display_name, url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kid, ws, connection_id, kind, remote_id, display_key,
                 display_name, url, _now()),
            )
        got = self.get_container(ws, kid)
        if got is None:  # pragma: no cover - defensive
            raise RuntimeError("container write did not persist")
        return got

    def get_container(self, workspace_id: str, container_id: str) -> Optional[ExternalContainer]:
        row = self._db.execute(
            "SELECT * FROM external_containers WHERE id = ? AND workspace_id = ?",
            (container_id, workspace_id),
        ).fetchone()
        return self._container(row) if row else None

    def list_containers(self, workspace_id: str, connection_id: Optional[str] = None) -> list[ExternalContainer]:
        sql = "SELECT * FROM external_containers WHERE workspace_id = ?"
        params: list[str] = [workspace_id]
        if connection_id is not None:
            sql += " AND connection_id = ?"
            params.append(connection_id)
        sql += " ORDER BY created_at, id"
        return [self._container(r) for r in self._db.execute(sql, params).fetchall()]

    # ── entities ──────────────────────────────────────────────────────
    def upsert_entity(
        self, workspace_id: str, connection_id: str, container_id: str,
        entity: ExternalEntityInput,
    ) -> tuple[ExternalEntity, bool]:
        """Upsert by exact opaque identity. Returns (entity, created).

        Display key/url/title are display-only and never dedupe. The raw remote
        status is preserved verbatim; the normalized category is derived and
        stored alongside for filtering (it never enters the conductor).
        """
        ws = _require(workspace_id, "workspace_id")
        if self.get_container(ws, container_id) is None:
            raise ValueError("container does not belong to this workspace")
        eid = entity_id_for(ws, connection_id, entity.entity_kind, entity.remote_id)
        category = entity.status_category or normalize_status_category(entity.remote_status)
        assignees = json.dumps(list(entity.assignees or ()))
        now = _now()
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO external_entities "
                "(id, workspace_id, connection_id, container_id, entity_kind, "
                "remote_id, display_key, title, body, url, remote_status, "
                "status_category, assignees, revision, remote_updated_at, "
                "last_seen_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, ws, connection_id, container_id, entity.entity_kind,
                 entity.remote_id, entity.display_key, entity.title, entity.body,
                 entity.url, entity.remote_status, category, assignees,
                 entity.revision, entity.remote_updated_at, now, now),
            )
            created = cur.rowcount == 1
            if not created:
                self._db.execute(
                    "UPDATE external_entities SET display_key = ?, title = ?, "
                    "body = ?, url = ?, remote_status = ?, status_category = ?, "
                    "assignees = ?, revision = ?, remote_updated_at = ?, "
                    "last_seen_at = ? WHERE id = ? AND workspace_id = ?",
                    (entity.display_key, entity.title, entity.body, entity.url,
                     entity.remote_status, category, assignees, entity.revision,
                     entity.remote_updated_at, now, eid, ws),
                )
        got = self.get_entity(ws, eid)
        if got is None:  # pragma: no cover - defensive
            raise RuntimeError("entity write did not persist")
        return got, created

    def get_entity(self, workspace_id: str, entity_id: str) -> Optional[ExternalEntity]:
        row = self._db.execute(
            "SELECT * FROM external_entities WHERE id = ? AND workspace_id = ?",
            (entity_id, workspace_id),
        ).fetchone()
        return self._entity(row) if row else None

    def list_entities(
        self, workspace_id: str, container_id: Optional[str] = None,
        connection_id: Optional[str] = None, task_id: Optional[str] = None,
    ) -> list[ExternalEntity]:
        sql = "SELECT e.* FROM external_entities e"
        params: list[str] = []
        if task_id is not None:
            sql += (" JOIN work_item_external_links l "
                    "ON l.entity_id = e.id AND l.workspace_id = e.workspace_id")
        sql += " WHERE e.workspace_id = ?"
        params.append(workspace_id)
        if container_id is not None:
            sql += " AND e.container_id = ?"
            params.append(container_id)
        if connection_id is not None:
            sql += " AND e.connection_id = ?"
            params.append(connection_id)
        if task_id is not None:
            sql += " AND l.task_id = ?"
            params.append(task_id)
        sql += " ORDER BY e.created_at, e.id"
        return [self._entity(r) for r in self._db.execute(sql, params).fetchall()]

    # ── messages (sibling of entities; NEVER touches intake, task 33798164) ──
    def upsert_message(
        self, workspace_id: str, connection_id: str, provider: str,
        message: ExternalMessageInput,
    ) -> tuple[ExternalMessage, bool]:
        """Upsert by exact opaque identity. Returns (message, created).

        Deliberately independent of external_entities/upsert_entity: no
        container, no WorkItemExternalLink, and no intake call anywhere in
        this method — a message can never mint a local task.
        """
        ws = _require(workspace_id, "workspace_id")
        if self.get_connection(ws, connection_id) is None:
            raise ValueError("connection does not belong to this workspace")
        mid = message_id_for(ws, connection_id, provider, message.remote_id)
        now = _now()
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO external_messages "
                "(id, workspace_id, connection_id, provider, remote_id, "
                "thread_id, sender, subject, body, remote_created_at, "
                "last_seen_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, ws, connection_id, provider, message.remote_id,
                 message.thread_id, message.sender, message.subject,
                 message.body, message.remote_created_at, now, now),
            )
            created = cur.rowcount == 1
            if not created:
                self._db.execute(
                    "UPDATE external_messages SET thread_id = ?, sender = ?, "
                    "subject = ?, body = ?, remote_created_at = ?, "
                    "last_seen_at = ? WHERE id = ? AND workspace_id = ?",
                    (message.thread_id, message.sender, message.subject,
                     message.body, message.remote_created_at, now, mid, ws),
                )
        got = self.get_message(ws, mid)
        if got is None:  # pragma: no cover - defensive
            raise RuntimeError("message write did not persist")
        return got, created

    def get_message(self, workspace_id: str, message_id: str) -> Optional[ExternalMessage]:
        row = self._db.execute(
            "SELECT * FROM external_messages WHERE id = ? AND workspace_id = ?",
            (message_id, workspace_id),
        ).fetchone()
        return self._message(row) if row else None

    def list_messages(
        self, workspace_id: str, connection_id: Optional[str] = None,
    ) -> list[ExternalMessage]:
        sql = "SELECT * FROM external_messages WHERE workspace_id = ?"
        params: list[str] = [workspace_id]
        if connection_id is not None:
            sql += " AND connection_id = ?"
            params.append(connection_id)
        sql += " ORDER BY created_at, id"
        return [self._message(r) for r in self._db.execute(sql, params).fetchall()]

    # ── links ─────────────────────────────────────────────────────────
    def claim_import_link(self, workspace_id: str, entity_id: str, task_id: str) -> WorkItemExternalLink:
        ws = _require(workspace_id, "workspace_id")
        lid = link_id_for(ws, entity_id)
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO work_item_external_links "
                "(id, workspace_id, entity_id, task_id, link_type, state, created_at) "
                "VALUES (?, ?, ?, ?, 'import', 'provisioning', ?)",
                (lid, ws, entity_id, task_id, _now()),
            )
        got = self.get_link(ws, lid)
        if got is None:  # pragma: no cover - defensive
            raise RuntimeError("link write did not persist")
        return got

    def activate_link(self, workspace_id: str, link_id: str) -> Optional[WorkItemExternalLink]:
        with self._db:
            self._db.execute(
                "UPDATE work_item_external_links SET state = 'active' "
                "WHERE id = ? AND workspace_id = ?",
                (link_id, workspace_id),
            )
        return self.get_link(workspace_id, link_id)

    def get_link(self, workspace_id: str, link_id: str) -> Optional[WorkItemExternalLink]:
        row = self._db.execute(
            "SELECT * FROM work_item_external_links WHERE id = ? AND workspace_id = ?",
            (link_id, workspace_id),
        ).fetchone()
        return self._link(row) if row else None

    def list_links(
        self, workspace_id: str, task_id: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> list[WorkItemExternalLink]:
        sql = "SELECT * FROM work_item_external_links WHERE workspace_id = ?"
        params: list[str] = [workspace_id]
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        if entity_id is not None:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        sql += " ORDER BY created_at, id"
        return [self._link(r) for r in self._db.execute(sql, params).fetchall()]

    # ── runs / receipts ───────────────────────────────────────────────
    def start_run(self, workspace_id: str, connection_id: str, container_id: str) -> SyncRun:
        ws = _require(workspace_id, "workspace_id")
        rid = str(uuid4())
        with self._db:
            self._db.execute(
                "INSERT INTO sync_runs "
                "(id, workspace_id, connection_id, container_id, status, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (rid, ws, connection_id, container_id, _now()),
            )
        return self.get_run(ws, rid)

    def finish_run(
        self, workspace_id: str, run_id: str, status: str,
        error_code: str = "", items_processed: int = 0,
    ) -> Optional[SyncRun]:
        with self._db:
            self._db.execute(
                "UPDATE sync_runs SET status = ?, error_code = ?, "
                "items_processed = ?, finished_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (status, error_code, int(items_processed), _now(), run_id, workspace_id),
            )
        return self.get_run(workspace_id, run_id)

    def get_run(self, workspace_id: str, run_id: str) -> Optional[SyncRun]:
        row = self._db.execute(
            "SELECT * FROM sync_runs WHERE id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        ).fetchone()
        return self._run(row) if row else None

    def list_runs(self, workspace_id: str, container_id: Optional[str] = None) -> list[SyncRun]:
        sql = "SELECT * FROM sync_runs WHERE workspace_id = ?"
        params: list[str] = [workspace_id]
        if container_id is not None:
            sql += " AND container_id = ?"
            params.append(container_id)
        # rowid DESC as the tie-break: two runs started inside the same
        # clock tick (Windows' coarse system clock trivially produces
        # identical started_at strings) must still resolve "latest" by
        # insertion order — the old `, id` tie-break was a RANDOM uuid, so
        # last_sync was a coin flip under a tied timestamp (flaked live in
        # test_jira_status_last_sync_is_the_most_recent_run, 2026-08-16).
        sql += " ORDER BY started_at DESC, rowid DESC"
        return [self._run(r) for r in self._db.execute(sql, params).fetchall()]

    def append_receipt(
        self, workspace_id: str, run_id: str, entity_id: str, outcome: str,
        error_code: str = "",
    ) -> SyncReceipt:
        ws = _require(workspace_id, "workspace_id")
        rid = str(uuid4())
        with self._db:
            self._db.execute(
                "INSERT INTO sync_receipts "
                "(id, workspace_id, run_id, entity_id, outcome, error_code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rid, ws, run_id, entity_id, outcome, error_code, _now()),
            )
        row = self._db.execute("SELECT * FROM sync_receipts WHERE id = ?", (rid,)).fetchone()
        return self._receipt(row)

    def list_receipts(self, workspace_id: str, run_id: Optional[str] = None) -> list[SyncReceipt]:
        sql = "SELECT * FROM sync_receipts WHERE workspace_id = ?"
        params: list[str] = [workspace_id]
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY created_at, id"
        return [self._receipt(r) for r in self._db.execute(sql, params).fetchall()]

    # ── cursors ───────────────────────────────────────────────────────
    def get_cursor(self, workspace_id: str, connection_id: str, container_id: str, stream: str = "default") -> Optional[str]:
        row = self._db.execute(
            "SELECT cursor FROM sync_cursors WHERE workspace_id = ? AND "
            "connection_id = ? AND container_id = ? AND stream = ?",
            (workspace_id, connection_id, container_id, stream),
        ).fetchone()
        return row["cursor"] if row else None

    def set_cursor(self, workspace_id: str, connection_id: str, container_id: str, stream: str, cursor: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO sync_cursors "
                "(workspace_id, connection_id, container_id, stream, cursor, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (workspace_id, connection_id, container_id, stream) "
                "DO UPDATE SET cursor = excluded.cursor, updated_at = excluded.updated_at",
                (workspace_id, connection_id, container_id, stream, cursor, _now()),
            )

    # ── row mappers ───────────────────────────────────────────────────
    @staticmethod
    def _connection(row: sqlite3.Row) -> IntegrationConnection:
        return IntegrationConnection(
            id=row["id"], workspace_id=row["workspace_id"], provider=row["provider"],
            remote_scope=row["remote_scope"], display_name=row["display_name"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _container(row: sqlite3.Row) -> ExternalContainer:
        return ExternalContainer(
            id=row["id"], workspace_id=row["workspace_id"],
            connection_id=row["connection_id"], kind=row["kind"],
            remote_id=row["remote_id"], display_key=row["display_key"],
            display_name=row["display_name"], url=row["url"], created_at=row["created_at"],
        )

    @staticmethod
    def _entity(row: sqlite3.Row) -> ExternalEntity:
        return ExternalEntity(
            id=row["id"], workspace_id=row["workspace_id"],
            connection_id=row["connection_id"], container_id=row["container_id"],
            entity_kind=row["entity_kind"], remote_id=row["remote_id"],
            display_key=row["display_key"], title=row["title"], body=row["body"],
            url=row["url"], remote_status=row["remote_status"],
            status_category=row["status_category"],
            assignees=json.loads(row["assignees"] or "[]"), revision=row["revision"],
            remote_updated_at=row["remote_updated_at"], last_seen_at=row["last_seen_at"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _message(row: sqlite3.Row) -> ExternalMessage:
        return ExternalMessage(
            id=row["id"], workspace_id=row["workspace_id"],
            connection_id=row["connection_id"], provider=row["provider"],
            remote_id=row["remote_id"], thread_id=row["thread_id"],
            sender=row["sender"], subject=row["subject"], body=row["body"],
            remote_created_at=row["remote_created_at"],
            last_seen_at=row["last_seen_at"], created_at=row["created_at"],
        )

    @staticmethod
    def _link(row: sqlite3.Row) -> WorkItemExternalLink:
        return WorkItemExternalLink(
            id=row["id"], workspace_id=row["workspace_id"], entity_id=row["entity_id"],
            task_id=row["task_id"], link_type=row["link_type"], state=row["state"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> SyncRun:
        return SyncRun(
            id=row["id"], workspace_id=row["workspace_id"],
            connection_id=row["connection_id"], container_id=row["container_id"],
            status=row["status"], error_code=row["error_code"],
            items_processed=row["items_processed"], started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _receipt(row: sqlite3.Row) -> SyncReceipt:
        return SyncReceipt(
            id=row["id"], workspace_id=row["workspace_id"], run_id=row["run_id"],
            entity_id=row["entity_id"], outcome=row["outcome"],
            error_code=row["error_code"], created_at=row["created_at"],
        )


# ── process-global singleton (transport adapters reach it) ─────────────────
_integration_store: Optional[IntegrationStore] = None
_lock = threading.Lock()


def set_integration_store(store: Optional[IntegrationStore]) -> None:
    global _integration_store
    with _lock:
        _integration_store = store


def get_integration_store() -> IntegrationStore:
    global _integration_store
    if _integration_store is None:
        with _lock:
            if _integration_store is None:
                _integration_store = IntegrationStore(
                    resolve_data_dir() / "integrations.db"
                )
    return _integration_store
