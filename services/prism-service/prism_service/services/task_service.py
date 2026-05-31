"""Task service — manages tasks in SQLite."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

from prism_service.models.task import Task, TaskHistory


# Callable signature for LL-03's embedder injection. Returns packed
# float32 bytes suitable for the `tasks.embedding` BLOB column, or
# ``None`` when no embedder is available (offline, first-session).
EmbedFn = Callable[[str], Optional[bytes]]


_CREATE_TASKS_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    story_file TEXT DEFAULT '',
    assigned_agent TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT DEFAULT '',
    completed_at TEXT DEFAULT '',
    blocked_reason TEXT DEFAULT '',
    dependencies TEXT DEFAULT '[]',
    tags TEXT DEFAULT '[]',
    embedding BLOB,
    merge_sha TEXT,
    merged_at TEXT,
    workflow_step TEXT DEFAULT '',
    gate_state TEXT DEFAULT 'none',
    gate_reason TEXT DEFAULT '',
    parent_id TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    actor TEXT DEFAULT '',
    action TEXT NOT NULL,
    details TEXT DEFAULT '',
    timestamp TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
"""


# Columns added by LL-01 (learning-loop schema migration). Applied via
# ALTER TABLE on existing DBs so older task rows don't need rewriting.
_LL_TASK_COLUMNS: list[tuple[str, str]] = [
    ("embedding", "BLOB"),
    ("merge_sha", "TEXT"),
    ("merged_at", "TEXT"),
    # Conductor v2 (issue #79 [1/4]) — per-task workflow state machine.
    # Existing rows backfill via SQLite DEFAULTs: workflow_step=''
    # (not yet in the workflow) and gate_state='none'.
    ("workflow_step", "TEXT DEFAULT ''"),
    ("gate_state", "TEXT DEFAULT 'none'"),
    ("gate_reason", "TEXT DEFAULT ''"),
    # Hierarchy (parent → children). Existing rows backfill to '' (root).
    ("parent_id", "TEXT DEFAULT ''"),
]


class TaskService:
    """Manages the tasks.db lifecycle and CRUD operations."""

    def __init__(
        self, db_path: str, embed_fn: Optional[EmbedFn] = None,
        scores_db: Optional[str] = None,
    ) -> None:
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_CREATE_TASKS_SQL)
        self._migrate_task_columns()
        # LL task-session association lives in scores.db (alongside
        # session_outcomes), not tasks.db — the JOIN the reader needs is
        # over that one file. None in unit contexts that never touch the
        # association surface; link_session / sessions_for_task degrade
        # gracefully (no-op writer, [] reader) when unset.
        self._scores_db: Optional[str] = scores_db
        # Optional — when provided, task create/update embeds
        # ``title + "\n" + description`` into ``tasks.embedding`` so
        # LL-06's cosine-similarity retrieval has vectors to work with.
        # Left as None in contexts where the embedder isn't loaded
        # (e.g. hook smoke tests); create/update still succeeds, the
        # row just lacks an embedding until re-indexed.
        self._embed_fn: Optional[EmbedFn] = embed_fn

    # ------------------------------------------------------------------
    # LL-03 helper: write an embedding for the given task if we can
    # ------------------------------------------------------------------

    @staticmethod
    def _embedding_text(title: str, description: str) -> str:
        return f"{title}\n{description}".strip()

    def _store_embedding(self, task_id: str, title: str, description: str) -> None:
        if self._embed_fn is None:
            return
        try:
            blob = self._embed_fn(self._embedding_text(title, description))
        except Exception:
            blob = None
        if blob is None:
            return
        self._db.execute(
            "UPDATE tasks SET embedding=? WHERE id=?", (blob, task_id)
        )
        self._db.commit()

    def _migrate_task_columns(self) -> None:
        """Backfill LL-01 columns on tasks.db files created before the
        learning-loop schema landed. Idempotent: ALTER is only issued
        when the column is actually missing."""
        existing = {
            row[1]
            for row in self._db.execute("PRAGMA table_info(tasks)").fetchall()
        }
        for col, col_type in _LL_TASK_COLUMNS:
            if col in existing:
                continue
            try:
                self._db.execute(
                    f"ALTER TABLE tasks ADD COLUMN {col} {col_type}"
                )
                self._db.commit()
            except sqlite3.OperationalError:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """Convert a database row to a Task dataclass."""
        # Conductor v2 columns are optional on the cursor for tests that
        # query against pre-migration handles; .keys() lookup keeps the
        # cast safe and falls back to dataclass defaults.
        keys = set(row.keys())
        return Task(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=row["status"],
            priority=row["priority"],
            story_file=row["story_file"],
            assigned_agent=row["assigned_agent"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            blocked_reason=row["blocked_reason"],
            dependencies=json.loads(row["dependencies"]),
            tags=json.loads(row["tags"]),
            workflow_step=(row["workflow_step"] if "workflow_step" in keys
                           and row["workflow_step"] is not None else ""),
            gate_state=(row["gate_state"] if "gate_state" in keys
                        and row["gate_state"] is not None else "none"),
            gate_reason=(row["gate_reason"] if "gate_reason" in keys
                         and row["gate_reason"] is not None else ""),
            parent_id=(row["parent_id"] if "parent_id" in keys
                       and row["parent_id"] is not None else ""),
        )

    def _record_history(
        self, task_id: str, action: str, details: str = "", actor: str = "",
    ) -> None:
        """Insert an audit row into task_history."""
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "INSERT INTO task_history (task_id, actor, action, details, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, actor, action, details, now),
        )
        self._db.commit()

    def record_history(
        self, task_id: str, action: str, details: str = "", actor: str = "",
    ) -> None:
        """Public wrapper around _record_history for collaborators
        (e.g. ConductorService) that need to append audit rows for
        workflow_step / gate transitions without going through update().
        """
        self._record_history(task_id, action, details=details, actor=actor)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        title: str,
        description: str = "",
        priority: int = 0,
        dependencies: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        story_file: str = "",
        assigned_agent: str = "",
        parent_id: str = "",
    ) -> Task:
        """Create a new task and return it."""
        task = Task(
            title=title,
            description=description,
            priority=priority,
            story_file=story_file,
            assigned_agent=assigned_agent,
            dependencies=dependencies or [],
            tags=tags or [],
            parent_id=parent_id,
        )
        self._db.execute(
            "INSERT INTO tasks "
            "(id, title, description, status, priority, story_file, "
            "assigned_agent, created_at, updated_at, completed_at, "
            "blocked_reason, dependencies, tags, parent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                task.title,
                task.description,
                task.status,
                task.priority,
                task.story_file,
                task.assigned_agent,
                task.created_at,
                task.updated_at,
                task.completed_at,
                task.blocked_reason,
                json.dumps(task.dependencies),
                json.dumps(task.tags),
                task.parent_id,
            ),
        )
        self._db.commit()
        self._record_history(task.id, "created", f"title={title!r}")
        # LL-03: embed title+description so LL-06's similarity retrieval
        # has something to search over. Silent on embedder-offline —
        # the row still exists, just without a vector.
        self._store_embedding(task.id, task.title, task.description)
        return task

    def get(self, task_id: str) -> Optional[Task]:
        """Fetch a single task by ID, or None."""
        row = self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def list(
        self,
        status: Optional[str] = None,
        assigned_agent: Optional[str] = None,
        tag: Optional[str] = None,
        story_file: Optional[str] = None,
    ) -> list[Task]:
        """List tasks with optional filters."""
        clauses: list[str] = []
        params: list[str] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if assigned_agent is not None:
            clauses.append("assigned_agent = ?")
            params.append(assigned_agent)
        if story_file is not None:
            clauses.append("story_file = ?")
            params.append(story_file)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._db.execute(
            f"SELECT * FROM tasks{where} ORDER BY priority DESC, created_at ASC",
            params,
        ).fetchall()

        tasks = [self._row_to_task(r) for r in rows]

        # Tag filtering is done in Python because tags are JSON-encoded
        if tag is not None:
            tasks = [t for t in tasks if tag in t.tags]

        return tasks

    def update(self, task_id: str, **kwargs: object) -> Optional[Task]:
        """Update arbitrary fields on a task. Records change history."""
        task = self.get(task_id)
        if task is None:
            return None

        now = datetime.now(timezone.utc).isoformat()
        changes: list[str] = []

        for key, value in kwargs.items():
            if not hasattr(task, key) or key == "id":
                continue
            old_value = getattr(task, key)
            if old_value == value:
                continue
            setattr(task, key, value)
            changes.append(f"{key}: {old_value!r} -> {value!r}")

        if not changes:
            return task

        task.updated_at = now

        # Auto-set completed_at when transitioning to done
        if kwargs.get("status") == "done" and not task.completed_at:
            task.completed_at = now

        self._db.execute(
            "UPDATE tasks SET title=?, description=?, status=?, priority=?, "
            "story_file=?, assigned_agent=?, updated_at=?, completed_at=?, "
            "blocked_reason=?, dependencies=?, tags=?, "
            "workflow_step=?, gate_state=?, gate_reason=?, parent_id=? "
            "WHERE id=?",
            (
                task.title,
                task.description,
                task.status,
                task.priority,
                task.story_file,
                task.assigned_agent,
                task.updated_at,
                task.completed_at,
                task.blocked_reason,
                json.dumps(task.dependencies),
                json.dumps(task.tags),
                task.workflow_step,
                task.gate_state,
                task.gate_reason,
                task.parent_id,
                task.id,
            ),
        )
        self._db.commit()
        self._record_history(task.id, "updated", "; ".join(changes))
        # LL-03: re-embed only when the title or description changed.
        # Priority / status / tag-only updates don't move the vector.
        if "title" in kwargs or "description" in kwargs:
            self._store_embedding(task.id, task.title, task.description)
        return task

    # ------------------------------------------------------------------
    # Next-task algorithm
    # ------------------------------------------------------------------

    def next_task(self) -> Optional[dict]:
        """Return the highest-priority unblocked pending task.

        Algorithm:
        1. Fetch all pending tasks.
        2. Filter out tasks whose dependencies are not all 'done'.
        3. Sort by priority DESC, created_at ASC.
        4. Return top result with a reason string.
        """
        pending = self.list(status="pending")
        if not pending:
            return None

        # Build set of done task IDs for dependency checks
        done_ids = {
            t.id for t in self.list(status="done")
        }

        unblocked = [
            t for t in pending
            if all(dep in done_ids for dep in t.dependencies)
        ]

        if not unblocked:
            return None

        # Already sorted by priority DESC, created_at ASC from list()
        best = unblocked[0]
        reason_parts = [f"priority={best.priority}"]
        if best.assigned_agent:
            reason_parts.append(f"assigned to {best.assigned_agent}")
        if best.story_file:
            reason_parts.append(f"story={best.story_file}")
        reason = "Highest priority unblocked task: " + ", ".join(reason_parts)

        return {"task": best, "reason": reason}

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def history(self, task_id: str) -> list[TaskHistory]:
        """Return audit history for a given task."""
        rows = self._db.execute(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY timestamp ASC",
            (task_id,),
        ).fetchall()
        return [
            TaskHistory(
                id=r["id"],
                task_id=r["task_id"],
                actor=r["actor"],
                action=r["action"],
                details=r["details"],
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Task <-> session association (LL — activates task_sessions)
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_task_sessions(conn: sqlite3.Connection) -> None:
        """Idempotently materialize the task_sessions table on an
        isolated scores.db handle — the schema normally ships from
        brain_engine, but the writer/reader may run before Brain has
        opened that DB (e.g. conductor-only flows)."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_sessions ("
            "task_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "started_at TEXT, ended_at TEXT, "
            "PRIMARY KEY (task_id, session_id))"
        )

    def link_session(
        self, task_id: str, session_id: str,
        ended_at: Optional[str] = None,
    ) -> bool:
        """Upsert a task_sessions row tying `session_id` to `task_id`.

        started_at semantics: stamped (UTC ISO8601) the FIRST time the
        pair is linked and never overwritten on a re-link — it marks
        when PRISM first observed the session working this task.
        ended_at is refreshed whenever supplied (session-end signal).
        Returns False (no-op) when no scores.db is bound.
        """
        if not self._scores_db:
            return False
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self._scores_db, timeout=5.0)
        try:
            self._ensure_task_sessions(conn)
            conn.execute(
                "INSERT INTO task_sessions "
                "(task_id, session_id, started_at, ended_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id, session_id) DO UPDATE SET "
                "ended_at=COALESCE(excluded.ended_at, task_sessions.ended_at)",
                (task_id, session_id, now, ended_at),
            )
            conn.commit()
        finally:
            conn.close()
        return True

    def sessions_for_task(self, task_id: str) -> list[dict]:
        """Return the sessions linked to `task_id`, each row LEFT JOINed
        against session_outcomes so the UI gets per-session timing +
        token + file + skill metrics in one shape:
          [{session_id, started_at, ended_at, duration_s, tokens_used,
            files_read, files_modified, skills_invoked}]
        Empty list when nothing is linked (backs the UI empty state).
        """
        if not self._scores_db:
            return []
        conn = sqlite3.connect(self._scores_db, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            self._ensure_task_sessions(conn)
            rows = conn.execute(
                "SELECT ts.session_id, ts.started_at, ts.ended_at, "
                "COALESCE(so.duration_s, 0) AS duration_s, "
                "COALESCE(so.tokens_used, 0) AS tokens_used, "
                "COALESCE(so.files_read, 0) AS files_read, "
                "COALESCE(so.files_modified, 0) AS files_modified, "
                "COALESCE(so.skills_invoked, 0) AS skills_invoked "
                "FROM task_sessions ts "
                "LEFT JOIN session_outcomes so "
                "ON so.session_id = ts.session_id "
                "WHERE ts.task_id = ? "
                "ORDER BY ts.started_at ASC",
                (task_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()
        return [dict(r) for r in rows]
