"""User service — the PRISM identity store (Slice C).

Clones the TaskService store shape (per-thread sqlite connection, idempotent
schema create + additive column migration, create/get/list/update), but the
db lives at the DATA_DIR level — NOT per-project — because a person spans
projects (scoping today is project-only via ?project=; a User is orthogonal).

NEVER stores a Jira token: `jira_account_id` holds only the linked account's
id/handle. Tokens stay in jira_auth.py's chmod-600 store and never leave the
process.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from prism_service.data_dir import resolve_data_dir
from prism_service.models.user import User


_CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    handle TEXT DEFAULT '',
    jira_account_id TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


# Additive columns, applied via ALTER TABLE on existing users.db files so
# older rows don't need rewriting. Empty today — the seam is here so future
# identity fields migrate the same idempotent way tasks.db does.
_USER_COLUMNS: list[tuple[str, str]] = [
    ("jira_account_id", "TEXT DEFAULT ''"),
]


class UserService:
    """Manages the users.db lifecycle and CRUD operations."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        # DATA_DIR-level, cross-project: a person spans projects, so the
        # store is NOT under projects/<id>/. Callers may pass an explicit
        # path (tests / isolation); default is resolve_data_dir()/users.db.
        self._db_path = db_path or str(resolve_data_dir() / "users.db")
        # Per-thread connection (mirrors TaskService task 0584addb): one
        # sqlite handle per thread so concurrent callers never share a
        # cursor. Schema create + migration run once on the constructing
        # thread; other threads open the same, already-migrated file.
        self._tlocal = threading.local()
        self._db.executescript(_CREATE_USERS_SQL)
        self._migrate_user_columns()

    # ------------------------------------------------------------------
    # Per-thread connection factory
    # ------------------------------------------------------------------

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

    def _migrate_user_columns(self) -> None:
        """Backfill additive columns on users.db files created before a
        given field landed. Idempotent: ALTER only when the column is
        actually missing."""
        existing = {
            row[1]
            for row in self._db.execute("PRAGMA table_info(users)").fetchall()
        }
        for col, col_type in _USER_COLUMNS:
            if col in existing:
                continue
            try:
                self._db.execute(
                    f"ALTER TABLE users ADD COLUMN {col} {col_type}"
                )
                self._db.commit()
            except sqlite3.OperationalError:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        keys = set(row.keys())
        return User(
            id=row["id"],
            name=row["name"],
            handle=row["handle"],
            jira_account_id=(row["jira_account_id"]
                             if "jira_account_id" in keys
                             and row["jira_account_id"] is not None else ""),
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self, name: str = "", handle: str = "", jira_account_id: str = "",
    ) -> User:
        """Create a new user (id like 'usr_xxxx') and return it."""
        user = User(name=name, handle=handle, jira_account_id=jira_account_id)
        self._db.execute(
            "INSERT INTO users (id, name, handle, jira_account_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user.id, user.name, user.handle, user.jira_account_id,
             user.created_at),
        )
        self._db.commit()
        return user

    def get(self, user_id: str) -> Optional[User]:
        """Fetch a single user by id, or None."""
        row = self._db.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return self._row_to_user(row) if row else None

    def list(self) -> list[User]:
        """List all users, newest first."""
        rows = self._db.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_user(r) for r in rows]

    def update(self, user_id: str, **kwargs: object) -> Optional[User]:
        """Update arbitrary fields on a user. Returns the updated user or
        None when the id is unknown. `id`/`created_at` are immutable."""
        user = self.get(user_id)
        if user is None:
            return None
        for key, value in kwargs.items():
            if key in ("id", "created_at") or not hasattr(user, key):
                continue
            setattr(user, key, value)
        self._db.execute(
            "UPDATE users SET name=?, handle=?, jira_account_id=? WHERE id=?",
            (user.name, user.handle, user.jira_account_id, user.id),
        )
        self._db.commit()
        return user

    def link_jira(self, user_id: str, account_id: str) -> Optional[User]:
        """Link a Jira account to a user by storing its account id/handle
        (NEVER a token). Returns the updated user, or None if unknown."""
        return self.update(user_id, jira_account_id=account_id)
