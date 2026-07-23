"""Global workspace, membership, and project-ownership registry."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from prism_service.models.workspace import (
    AuthToken,
    Membership,
    ProjectOwnership,
    User,
    VALID_ROLES,
    Workspace,
    role_allows,
)
from prism_service.data_dir import resolve_data_dir
from prism_service.services import sqlite_db


class AuthorizationDenied(PermissionError):
    """Raised when a user lacks the required workspace role."""


class ProjectOwnershipConflict(RuntimeError):
    """Raised when code attempts to move an already-bound project."""


class BootstrapAlreadyCompleted(RuntimeError):
    """Raised when a caller tries to create a second initial team owner."""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (owner_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS memberships (
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_ownership (
    project_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memberships_user
    ON memberships(user_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_project_ownership_workspace
    ON project_ownership(workspace_id, project_id);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_hash
    ON auth_tokens(token_hash);

CREATE TRIGGER IF NOT EXISTS project_ownership_workspace_is_immutable
BEFORE UPDATE OF workspace_id ON project_ownership
WHEN OLD.workspace_id <> NEW.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'project ownership is immutable');
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(value: str, field: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


class WorkspaceService:
    """Own the process-global authorization database.

    The database itself is global rather than project-local because it is
    the authority that decides which workspace owns a project.  Connection
    handles are cached per thread so concurrent request workers never share
    a SQLite connection.
    """

    def __init__(self, db_path: "str | Path") -> None:
        self._db_path = str(db_path)
        self._tlocal = threading.local()
        self._db.executescript(_SCHEMA_SQL)
        self._migrate()

    def _migrate(self) -> None:
        """Column adds the CREATE TABLE IF NOT EXISTS schema cannot apply to an
        existing db (there is no migration runner — task e8059640/6cef97ec).
        Idempotent: each ALTER is guarded by a pragma check.

        `secret`: the owner reversed the shown-once model (decision mx-935cc2 /
        mx-ba4111) — "we can store the key, and I can get it any time I am
        logged in." So the key is recoverable. But a raw bearer must never
        touch disk in the clear (test_team_tokens_are_hashed_at_rest), so the
        column holds Fernet CIPHERTEXT, decrypted only in-process for the
        signed-in owner. token_hash stays for the fast bearer lookup."""
        cols = {
            r["name"]
            for r in self._db.execute("PRAGMA table_info(auth_tokens)")
        }
        if "secret" not in cols:
            self._db.execute("ALTER TABLE auth_tokens ADD COLUMN secret TEXT NOT NULL DEFAULT ''")
            self._db.commit()

    @property
    def _fernet(self):
        """Per-instance key-encryption key, in a file beside the db (the data
        dir), never the repo and never the db itself. Readable-at-rest is not
        the same as plaintext-at-rest: the stored access-key secret is
        encrypted with this, so a db dump or backup leaks ciphertext, not the
        key."""
        f = getattr(self, "_fernet_cache", None)
        if f is not None:
            return f
        from cryptography.fernet import Fernet
        key_path = Path(self._db_path).with_name(".token_key")
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

    def _encrypt_secret(self, secret: str) -> str:
        if not secret:
            return ""
        return self._fernet.encrypt(secret.encode("utf-8")).decode("ascii")

    def _decrypt_secret(self, stored: str) -> Optional[str]:
        """Plaintext for a stored ciphertext, or None when it cannot be
        decrypted (a pre-encryption plaintext row, or a foreign key) — the
        caller then treats it as no readable key and mints a fresh one."""
        if not stored:
            return None
        try:
            from cryptography.fernet import InvalidToken
            try:
                return self._fernet.decrypt(stored.encode("ascii")).decode("utf-8")
            except InvalidToken:
                return None
        except Exception:
            return None

    @property
    def _db(self) -> sqlite3.Connection:
        """Return the calling thread's canonical SQLite connection."""

        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = sqlite_db.connect(self._db_path, timeout=5.0)
            conn.execute("PRAGMA foreign_keys=ON")
            self._tlocal.conn = conn
        return conn

    def close(self) -> None:
        """Close this thread's handle; other threads own their own handles."""

        conn = getattr(self._tlocal, "conn", None)
        if conn is not None:
            conn.close()
            del self._tlocal.conn

    @staticmethod
    def _user_from_row(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _workspace_from_row(row: sqlite3.Row) -> Workspace:
        return Workspace(
            id=row["id"],
            name=row["name"],
            owner_user_id=row["owner_user_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _membership_from_row(row: sqlite3.Row) -> Membership:
        return Membership(
            workspace_id=row["workspace_id"],
            user_id=row["user_id"],
            role=row["role"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _ownership_from_row(row: sqlite3.Row) -> ProjectOwnership:
        return ProjectOwnership(
            project_id=row["project_id"],
            workspace_id=row["workspace_id"],
            created_at=row["created_at"],
        )

    def has_users(self) -> bool:
        row = self._db.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        return row is not None

    def bootstrap_owner(
        self,
        email: str,
        display_name: str,
        workspace_name: str,
        user_id: str,
        workspace_id: str,
        token_id: str,
        token_hash: str,
        token_label: str,
    ) -> tuple[User, Workspace, AuthToken]:
        """Atomically create the initial owner, workspace, and API token.

        ``BEGIN IMMEDIATE`` acquires SQLite's writer reservation before the
        singleton check.  Concurrent bootstrap requests therefore serialize:
        the winner commits the complete identity graph and the loser observes
        that user while it still holds the same write lock.  Any late failure
        (including token insertion) rolls every earlier insert back.
        """

        normalized_email = _required(email, "email").lower()
        normalized_display_name = str(display_name or "").strip()
        normalized_workspace_name = _required(workspace_name, "workspace_name")
        resolved_user_id = _required(user_id, "user_id")
        resolved_workspace_id = _required(workspace_id, "workspace_id")
        resolved_token_id = _required(token_id, "token_id")
        resolved_token_hash = _required(token_hash, "token_hash")
        normalized_token_label = str(token_label or "").strip()
        created_at = _now()

        conn = self._db
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
                raise BootstrapAlreadyCompleted(
                    "team identity has already been bootstrapped"
                )
            conn.execute(
                """
                INSERT INTO users (id, email, display_name, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    resolved_user_id,
                    normalized_email,
                    normalized_display_name,
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO workspaces (id, name, owner_user_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    resolved_workspace_id,
                    normalized_workspace_name,
                    resolved_user_id,
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO memberships (workspace_id, user_id, role, created_at)
                VALUES (?, ?, 'owner', ?)
                """,
                (resolved_workspace_id, resolved_user_id, created_at),
            )
            conn.execute(
                """
                INSERT INTO auth_tokens
                    (id, user_id, token_hash, label, created_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, '')
                """,
                (
                    resolved_token_id,
                    resolved_user_id,
                    resolved_token_hash,
                    normalized_token_label,
                    created_at,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return (
            User(
                resolved_user_id,
                normalized_email,
                normalized_display_name,
                created_at,
            ),
            Workspace(
                resolved_workspace_id,
                normalized_workspace_name,
                resolved_user_id,
                created_at,
            ),
            AuthToken(
                resolved_token_id,
                resolved_user_id,
                normalized_token_label,
                created_at,
                "",
            ),
        )

    def create_user(
        self,
        email: str,
        display_name: str = "",
        user_id: Optional[str] = None,
    ) -> User:
        normalized_email = _required(email, "email").lower()
        resolved_id = _required(user_id or str(uuid4()), "user_id")
        created_at = _now()
        try:
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO users (id, email, display_name, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (resolved_id, normalized_email, display_name.strip(), created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("user id or email already exists") from exc
        return User(resolved_id, normalized_email, display_name.strip(), created_at)

    def get_user(self, user_id: str) -> Optional[User]:
        row = self._db.execute(
            "SELECT id, email, display_name, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return self._user_from_row(row) if row is not None else None

    def get_user_by_email(self, email: str) -> Optional[User]:
        normalized_email = str(email).strip().lower()
        if not normalized_email:
            return None
        row = self._db.execute(
            """
            SELECT id, email, display_name, created_at
            FROM users
            WHERE email = ? COLLATE NOCASE
            """,
            (normalized_email,),
        ).fetchone()
        return self._user_from_row(row) if row is not None else None

    def create_workspace(
        self,
        name: str,
        owner_user_id: str,
        workspace_id: Optional[str] = None,
    ) -> Workspace:
        normalized_name = _required(name, "name")
        owner_id = _required(owner_user_id, "owner_user_id")
        if self.get_user(owner_id) is None:
            raise ValueError(f"unknown owner user: {owner_id}")
        resolved_id = _required(workspace_id or str(uuid4()), "workspace_id")
        created_at = _now()
        try:
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO workspaces (id, name, owner_user_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (resolved_id, normalized_name, owner_id, created_at),
                )
                self._db.execute(
                    """
                    INSERT INTO memberships (workspace_id, user_id, role, created_at)
                    VALUES (?, ?, 'owner', ?)
                    """,
                    (resolved_id, owner_id, created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("workspace id already exists") from exc
        return Workspace(resolved_id, normalized_name, owner_id, created_at)

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        row = self._db.execute(
            """
            SELECT id, name, owner_user_id, created_at
            FROM workspaces
            WHERE id = ?
            """,
            (workspace_id,),
        ).fetchone()
        return self._workspace_from_row(row) if row is not None else None

    def add_membership(
        self,
        workspace_id: str,
        user_id: str,
        role: str,
    ) -> Membership:
        if role not in VALID_ROLES:
            raise ValueError(
                f"invalid role {role!r}; expected one of {sorted(VALID_ROLES)}"
            )
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise ValueError(f"unknown workspace: {workspace_id}")
        if self.get_user(user_id) is None:
            raise ValueError(f"unknown user: {user_id}")
        if user_id == workspace.owner_user_id and role != "owner":
            raise ValueError("the workspace owner must retain the owner role")

        created_at = _now()
        with self._db:
            self._db.execute(
                """
                INSERT INTO memberships (workspace_id, user_id, role, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id, user_id)
                DO UPDATE SET role = excluded.role
                """,
                (workspace_id, user_id, role, created_at),
            )
        membership = self.membership_for(workspace_id, user_id)
        if membership is None:  # Defensive: the just-written row must exist.
            raise RuntimeError("membership write did not persist")
        return membership

    def membership_for(
        self,
        workspace_id: str,
        user_id: str,
    ) -> Optional[Membership]:
        row = self._db.execute(
            """
            SELECT workspace_id, user_id, role, created_at
            FROM memberships
            WHERE workspace_id = ? AND user_id = ?
            """,
            (workspace_id, user_id),
        ).fetchone()
        return self._membership_from_row(row) if row is not None else None

    def list_memberships(self, workspace_id: str) -> list[Membership]:
        rows = self._db.execute(
            """
            SELECT workspace_id, user_id, role, created_at
            FROM memberships
            WHERE workspace_id = ?
            ORDER BY user_id
            """,
            (workspace_id,),
        ).fetchall()
        return [self._membership_from_row(row) for row in rows]

    def list_memberships_for_user(self, user_id: str) -> list[Membership]:
        rows = self._db.execute(
            """
            SELECT workspace_id, user_id, role, created_at
            FROM memberships
            WHERE user_id = ?
            ORDER BY workspace_id
            """,
            (user_id,),
        ).fetchall()
        return [self._membership_from_row(row) for row in rows]

    def list_workspaces_for_user(self, user_id: str) -> list[Workspace]:
        rows = self._db.execute(
            """
            SELECT w.id, w.name, w.owner_user_id, w.created_at
            FROM workspaces AS w
            JOIN memberships AS m ON m.workspace_id = w.id
            WHERE m.user_id = ?
            ORDER BY w.name COLLATE NOCASE, w.id
            """,
            (user_id,),
        ).fetchall()
        return [self._workspace_from_row(row) for row in rows]

    def require_workspace_role(
        self,
        user_id: str,
        workspace_id: str,
        minimum_role: str,
    ) -> Membership:
        membership = self.membership_for(workspace_id, user_id)
        if membership is None or not role_allows(membership.role, minimum_role):
            raise AuthorizationDenied(
                f"user {user_id!r} lacks {minimum_role!r} access to "
                f"workspace {workspace_id!r}"
            )
        return membership

    @staticmethod
    def _canonical_project_id(project_id: str) -> str:
        # Lazy import avoids an api -> service -> api cycle.  The security
        # module deliberately keeps its canonicalizer independent of services.
        from prism_service.api.security import canonical_project_id

        return canonical_project_id(project_id)

    def reserve_project(
        self, project_id: str, workspace_id: str
    ) -> tuple[ProjectOwnership, bool]:
        """Atomically reserve a canonical project ID for one workspace.

        Returns ``(ownership, created)``.  A retry by the winning workspace is
        idempotent; a competing workspace receives a conflict before it can
        begin filesystem, clone, or worker side effects.
        """

        resolved_project_id = self._canonical_project_id(project_id)
        resolved_workspace_id = _required(workspace_id, "workspace_id")
        if self.get_workspace(resolved_workspace_id) is None:
            raise ValueError(f"unknown workspace: {resolved_workspace_id}")

        created_at = _now()
        with self._db:
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO project_ownership
                    (project_id, workspace_id, created_at)
                VALUES (?, ?, ?)
                """,
                (resolved_project_id, resolved_workspace_id, created_at),
            )
            row = self._db.execute(
                """
                SELECT project_id, workspace_id, created_at
                FROM project_ownership
                WHERE project_id = ?
                """,
                (resolved_project_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("project binding write did not persist")
        ownership = self._ownership_from_row(row)
        if ownership.workspace_id != resolved_workspace_id:
            raise ProjectOwnershipConflict(
                f"project {resolved_project_id!r} already belongs to workspace "
                f"{ownership.workspace_id!r}"
            )
        return ownership, cursor.rowcount > 0

    def bind_project(self, project_id: str, workspace_id: str) -> ProjectOwnership:
        ownership, _created = self.reserve_project(project_id, workspace_id)
        return ownership

    def project_workspace(self, project_id: str) -> Optional[Workspace]:
        resolved_project_id = self._canonical_project_id(project_id)
        row = self._db.execute(
            """
            SELECT w.id, w.name, w.owner_user_id, w.created_at
            FROM project_ownership AS p
            JOIN workspaces AS w ON w.id = p.workspace_id
            WHERE p.project_id = ?
            """,
            (resolved_project_id,),
        ).fetchone()
        return self._workspace_from_row(row) if row is not None else None

    def require_project_role(
        self,
        user_id: str,
        project_id: str,
        minimum_role: str,
    ) -> Membership:
        workspace = self.project_workspace(project_id)
        if workspace is None:
            raise AuthorizationDenied(f"project {project_id!r} has no workspace binding")
        return self.require_workspace_role(user_id, workspace.id, minimum_role)

    def list_projects_for_user(self, user_id: str) -> list[str]:
        rows = self._db.execute(
            """
            SELECT p.project_id
            FROM project_ownership AS p
            JOIN memberships AS m ON m.workspace_id = p.workspace_id
            WHERE m.user_id = ?
            ORDER BY p.project_id
            """,
            (user_id,),
        ).fetchall()
        return [row["project_id"] for row in rows]

    def store_auth_token(
        self,
        token_id: str,
        user_id: str,
        token_hash: str,
        label: str = "",
        secret: str = "",
    ) -> AuthToken:
        if self.get_user(user_id) is None:
            raise ValueError(f"unknown user: {user_id}")
        created_at = _now()
        with self._db:
            self._db.execute(
                """
                INSERT INTO auth_tokens
                    (id, user_id, token_hash, label, created_at, revoked_at, secret)
                VALUES (?, ?, ?, ?, ?, '', ?)
                """,
                (token_id, user_id, token_hash, label.strip(), created_at,
                 self._encrypt_secret(secret)),
            )
        return AuthToken(token_id, user_id, label.strip(), created_at, "")

    def active_key_for_user(self, user_id: str) -> Optional[dict]:
        """The user's current readable access key (task 6cef97ec) — the newest
        non-revoked token whose stored secret DECRYPTS. Returns
        {id, secret, label, created_at} or None. Served only to the
        authenticated owner of the key, never listed cross-user. Rows whose
        ciphertext cannot be decrypted (pre-encryption plaintext, foreign key)
        are skipped so the caller mints a fresh, encrypted one."""
        rows = self._db.execute(
            """
            SELECT id, secret, label, created_at
            FROM auth_tokens
            WHERE user_id = ? AND revoked_at = '' AND secret <> ''
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()
        for row in rows:
            plain = self._decrypt_secret(row["secret"])
            if plain is None:
                continue
            return {
                "id": row["id"],
                "secret": plain,
                "label": row["label"],
                "created_at": row["created_at"],
            }
        return None

    def user_for_token_hash(self, token_hash: str) -> Optional[User]:
        row = self._db.execute(
            """
            SELECT u.id, u.email, u.display_name, u.created_at
            FROM auth_tokens AS t
            JOIN users AS u ON u.id = t.user_id
            WHERE t.token_hash = ? AND t.revoked_at = ''
            """,
            (token_hash,),
        ).fetchone()
        return self._user_from_row(row) if row is not None else None

    def revoke_token(self, token_id: str, user_id: Optional[str] = None) -> bool:
        params: list[str] = [_now(), token_id]
        user_clause = ""
        if user_id is not None:
            user_clause = " AND user_id = ?"
            params.append(user_id)
        with self._db:
            cursor = self._db.execute(
                """
                UPDATE auth_tokens
                SET revoked_at = ?
                WHERE id = ? AND revoked_at = ''
                """
                + user_clause,
                params,
            )
        return cursor.rowcount > 0


_workspace_service: Optional[WorkspaceService] = None
_workspace_service_lock = threading.Lock()


def set_workspace_service(service: Optional[WorkspaceService]) -> None:
    """Set the process-global registry used by transport adapters."""

    global _workspace_service
    with _workspace_service_lock:
        _workspace_service = service


def get_workspace_service() -> WorkspaceService:
    """Return the process registry, lazily rooted in PRISM's data directory."""

    global _workspace_service
    if _workspace_service is None:
        with _workspace_service_lock:
            if _workspace_service is None:
                _workspace_service = WorkspaceService(
                    resolve_data_dir() / "workspace.db"
                )
    return _workspace_service
