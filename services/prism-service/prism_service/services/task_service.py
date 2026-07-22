"""Task service — manages tasks in SQLite."""

from __future__ import annotations

import json
import sqlite3
from prism_service.services import sqlite_db
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from prism_service.models.task import Task, TaskHistory


# Callable signature for LL-03's embedder injection. Returns packed
# float32 bytes suitable for the `tasks.embedding` BLOB column, or
# ``None`` when no embedder is available (offline, first-session).
EmbedFn = Callable[[str], Optional[bytes]]


# Conductor session gate (task ef81fc15): the ONE message every public
# surface (REST create/patch/advance + MCP task_update) raises when a
# sessionless task would be handed to the conductor. A conductor tile
# without a linked session is FROZEN — no transcript, no tokens, no live
# signals — so the gate refuses the transition and names the fix.
SESSION_GATE_FIX = (
    "a task cannot enter the conductor without a linked session — "
    "link a session first: POST /api/tasks/{task_id}/sessions "
    "(body: {\"session_id\": ...}) or the MCP task_link_session verb, "
    "then retry"
)


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
    parent_id TEXT DEFAULT '',
    oracle TEXT DEFAULT '',
    proof_type TEXT DEFAULT '',
    completion_proof TEXT DEFAULT '',
    likely_misfire TEXT DEFAULT '',
    full_outcome_complete INTEGER DEFAULT 0,
    allowed_files TEXT DEFAULT '[]',
    verify TEXT DEFAULT '[]',
    stop_if TEXT DEFAULT '[]',
    plan_doc TEXT DEFAULT '',
    plan_diagram TEXT DEFAULT ''
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


# Hot-query indexes (sqlite-hardening workstream). IF NOT EXISTS and
# executed on EVERY startup so existing tasks.db files pick them up —
# but only AFTER _migrate_task_columns(), because a legacy pre-
# Conductor-v2 store has no parent_id column yet and index-before-
# migration raised "no such column: parent_id".
_CREATE_INDEXES_SQL = """
-- history(): SELECT ... WHERE task_id=? ORDER BY timestamp ASC — the
-- per-task audit trail is read on every task detail view.
CREATE INDEX IF NOT EXISTS idx_task_history_task_ts
    ON task_history(task_id, timestamp);
-- list(status=...): the board's dominant filter.
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
-- list(parent_id=...): root-vs-children scoping on every board load.
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
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
    # Oracle (goalbuddy-ported): upfront observable completion signal.
    ("oracle", "TEXT DEFAULT ''"),
    ("proof_type", "TEXT DEFAULT ''"),
    ("completion_proof", "TEXT DEFAULT ''"),
    # likely_misfire (goalbuddy GAP-2): audited "pass-but-wrong" risk.
    ("likely_misfire", "TEXT DEFAULT ''"),
    # full_outcome_complete (goalbuddy GAP-4): owner-outcome vs slice. 0/1.
    ("full_outcome_complete", "INTEGER DEFAULT 0"),
    # Worker contract (goalbuddy T003): JSON-encoded list columns.
    ("allowed_files", "TEXT DEFAULT '[]'"),
    ("verify", "TEXT DEFAULT '[]'"),
    ("stop_if", "TEXT DEFAULT '[]'"),
    # Rich plan rendering — markdown proposed-change + Mermaid diagram
    # stored on the task. Existing rows backfill to '' (no plan => the
    # SPA keeps the current description view).
    ("plan_doc", "TEXT DEFAULT ''"),
    ("plan_diagram", "TEXT DEFAULT ''"),
]


class TaskService:
    """Manages the tasks.db lifecycle and CRUD operations."""

    def __init__(
        self, db_path: str, embed_fn: Optional[EmbedFn] = None,
        scores_db: Optional[str] = None,
    ) -> None:
        # THREAD-SAFE STORAGE (task 0584addb, PR #196 follow-up): one
        # sqlite connection PER THREAD via a thread-local factory — the
        # old single shared handle (check_same_thread=False) let
        # concurrent DriveEngine drives interleave statements/commits
        # (None reads, "cannot commit - no transaction is active").
        # Explicitly NOT a global serialize-everything lock (would kill
        # the fan-out concurrency) and NOT WAL-only (WAL + busy_timeout
        # are complements below; the per-thread handle is the fix).
        self._db_path = db_path
        self._tlocal = threading.local()
        # Schema create + column migration run ONCE here, on the
        # constructing thread's connection; other threads' lazy
        # connections open the same, already-migrated db file.
        self._db.executescript(_CREATE_TASKS_SQL)
        self._migrate_task_columns()
        # Indexes AFTER the column migration — idx_tasks_parent covers
        # parent_id, which a legacy db only gains via the ALTERs above.
        self._db.executescript(_CREATE_INDEXES_SQL)
        # LL task-session association lives in scores.db (alongside
        # session_outcomes), not tasks.db — the JOIN the reader needs is
        # over that one file. None in unit contexts that never touch the
        # association surface; link_session / sessions_for_task degrade
        # gracefully (no-op writer, [] reader) when unset.
        self._scores_db: Optional[str] = scores_db
        # In-memory fallback for the task<->session association when no
        # scores.db is bound (unit contexts). Keeps link_session /
        # sessions_for_task honest — the NO-SELF-OVERRIDE actor guard
        # (task 3826dac3) must still see who produced the work even when
        # the durable task_sessions table is absent.
        self._session_links: dict[str, dict[str, dict]] = {}
        # Optional — when provided, task create/update embeds
        # ``title + "\n" + description`` into ``tasks.embedding`` so
        # LL-06's cosine-similarity retrieval has vectors to work with.
        # Left as None in contexts where the embedder isn't loaded
        # (e.g. hook smoke tests); create/update still succeeds, the
        # row just lacks an embedding until re-indexed.
        self._embed_fn: Optional[EmbedFn] = embed_fn

    # ------------------------------------------------------------------
    # Per-thread connection factory (task 0584addb)
    # ------------------------------------------------------------------

    @property
    def _db(self) -> sqlite3.Connection:
        """The CALLING thread's connection to tasks.db, opened lazily and
        cached in a thread-local — every method body (and the tests that
        poke ``svc._db`` directly) keeps reading ``self._db`` unchanged,
        but no two threads ever share a handle. WAL lets readers overlap
        the single writer; busy_timeout queues cross-connection writes
        instead of erroring. Connections belonging to finished threads
        are reclaimed with their thread-local slot at GC."""
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = sqlite_db.connect(self._db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._tlocal.conn = conn
        return conn

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
            oracle=(row["oracle"] if "oracle" in keys
                    and row["oracle"] is not None else ""),
            proof_type=(row["proof_type"] if "proof_type" in keys
                        and row["proof_type"] is not None else ""),
            completion_proof=(row["completion_proof"]
                              if "completion_proof" in keys
                              and row["completion_proof"] is not None else ""),
            likely_misfire=(row["likely_misfire"]
                            if "likely_misfire" in keys
                            and row["likely_misfire"] is not None else ""),
            full_outcome_complete=bool(
                row["full_outcome_complete"]
                if "full_outcome_complete" in keys
                and row["full_outcome_complete"] is not None else 0),
            allowed_files=(json.loads(row["allowed_files"])
                           if "allowed_files" in keys
                           and row["allowed_files"] else []),
            verify=(json.loads(row["verify"])
                    if "verify" in keys and row["verify"] else []),
            stop_if=(json.loads(row["stop_if"])
                     if "stop_if" in keys and row["stop_if"] else []),
            plan_doc=(row["plan_doc"] if "plan_doc" in keys
                      and row["plan_doc"] is not None else ""),
            plan_diagram=(row["plan_diagram"] if "plan_diagram" in keys
                          and row["plan_diagram"] is not None else ""),
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
        oracle: str = "",
        proof_type: str = "",
        completion_proof: str = "",
        likely_misfire: str = "",
        full_outcome_complete: bool = False,
        allowed_files: Optional[list[str]] = None,
        verify: Optional[list[str]] = None,
        stop_if: Optional[list[str]] = None,
        plan_doc: str = "",
        plan_diagram: str = "",
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
            oracle=oracle,
            proof_type=proof_type,
            completion_proof=completion_proof,
            likely_misfire=likely_misfire,
            full_outcome_complete=full_outcome_complete,
            allowed_files=allowed_files or [],
            verify=verify or [],
            stop_if=stop_if or [],
            plan_doc=plan_doc,
            plan_diagram=plan_diagram,
        )
        self._db.execute(
            "INSERT INTO tasks "
            "(id, title, description, status, priority, story_file, "
            "assigned_agent, created_at, updated_at, completed_at, "
            "blocked_reason, dependencies, tags, parent_id, "
            "oracle, proof_type, completion_proof, likely_misfire, "
            "full_outcome_complete, "
            "allowed_files, verify, stop_if, plan_doc, plan_diagram) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                task.oracle,
                task.proof_type,
                task.completion_proof,
                task.likely_misfire,
                int(bool(task.full_outcome_complete)),
                json.dumps(task.allowed_files),
                json.dumps(task.verify),
                json.dumps(task.stop_if),
                task.plan_doc,
                task.plan_diagram,
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
        parent_id: Optional[str] = None,
        id: Optional[str] = None,
    ) -> list[Task]:
        """List tasks with optional filters.

        ``parent_id`` (FR-6, task 0e071d68) scopes to one epic's children:
        pass an epic id to return only its direct children, or '' to return
        only root tasks. None (default) does not filter on parentage.

        ``id`` scopes to a SINGLE task — a by-id read so a caller (e.g. the
        implement drive) reads just the one task it is working instead of the
        whole board (the dominant token sink: a full board is ~100x larger).
        """
        clauses: list[str] = []
        params: list[str] = []

        if id is not None:
            clauses.append("id = ?")
            params.append(id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if assigned_agent is not None:
            clauses.append("assigned_agent = ?")
            params.append(assigned_agent)
        if story_file is not None:
            clauses.append("story_file = ?")
            params.append(story_file)
        if parent_id is not None:
            clauses.append("parent_id = ?")
            params.append(parent_id)

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
            # Blank-title guard (customer bug 11040b39): a None / empty /
            # whitespace-only title is ignored so a rename can never blank
            # out a task's title. NOT NULL on the column would also reject
            # None — this keeps the existing title instead of erroring.
            if key == "title" and not (value or "").strip():
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
            "workflow_step=?, gate_state=?, gate_reason=?, parent_id=?, "
            "oracle=?, proof_type=?, completion_proof=?, likely_misfire=?, "
            "full_outcome_complete=?, "
            "allowed_files=?, verify=?, stop_if=?, "
            "plan_doc=?, plan_diagram=? "
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
                task.oracle,
                task.proof_type,
                task.completion_proof,
                task.likely_misfire,
                int(bool(task.full_outcome_complete)),
                json.dumps(task.allowed_files),
                json.dumps(task.verify),
                json.dumps(task.stop_if),
                task.plan_doc,
                task.plan_diagram,
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

    @staticmethod
    def _is_truncated_prefix(session_id: str, existing) -> bool:
        """True when ``session_id`` is a strict, SHORTER prefix of an
        already-linked session id for the same task — a truncated phantom
        (e.g. the 8-char "6d42fcd4" prefix of the full "6d42fcd4-...-..."
        UUID) that would otherwise render as a duplicate zero-stat row
        (task 7bdb5701)."""
        sid = session_id or ""
        for other in existing:
            o = other or ""
            if o != sid and len(sid) < len(o) and o.startswith(sid):
                return True
        return False

    def link_session(
        self, task_id: str, session_id: str,
        ended_at: Optional[str] = None,
    ) -> bool:
        """Upsert a task_sessions row tying `session_id` to `task_id`.

        started_at semantics: stamped (UTC ISO8601) the FIRST time the
        pair is linked and never overwritten on a re-link — it marks
        when PRISM first observed the session working this task.
        ended_at is refreshed whenever supplied (session-end signal).
        Falls back to an in-memory link when no scores.db is bound so the
        association (and the NO-SELF-OVERRIDE actor guard reading it) still
        works in unit contexts.
        """
        now = datetime.now(timezone.utc).isoformat()
        if not (session_id or "").strip():
            return False
        if not self._scores_db:
            task_links = self._session_links.setdefault(task_id, {})
            # Reject a truncated-prefix phantom (e.g. an 8-char "6d42fcd4"
            # alongside the real "6d42fcd4-...-..." UUID) that would render as
            # a duplicate zero-stat session row (task 7bdb5701).
            if self._is_truncated_prefix(session_id, task_links.keys()):
                return False
            row = task_links.setdefault(
                session_id, {"session_id": session_id, "started_at": now,
                             "ended_at": None})
            if ended_at is not None:
                row["ended_at"] = ended_at
            return True
        conn = sqlite_db.connect(self._scores_db, timeout=5.0)
        try:
            self._ensure_task_sessions(conn)
            existing = [
                r[0] for r in conn.execute(
                    "SELECT session_id FROM task_sessions WHERE task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            if self._is_truncated_prefix(session_id, existing):
                return False
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
            return [
                {"session_id": r["session_id"], "started_at": r["started_at"],
                 "ended_at": r["ended_at"], "duration_s": 0, "tokens_used": 0,
                 "files_read": 0, "files_modified": 0, "skills_invoked": 0}
                for r in self._session_links.get(task_id, {}).values()
            ]
        conn = sqlite_db.connect(self._scores_db, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            self._ensure_task_sessions(conn)
            # session_outcomes is Brain-owned schema — on a scores.db where
            # no session outcome was ever recorded the table is absent and
            # the LEFT JOIN raises OperationalError, which used to be
            # swallowed into a LYING empty list (a real task_sessions link
            # read as "no sessions" — the conductor session gate would then
            # refuse a correctly-linked task). Degrade to the bare
            # task_sessions rows with zeroed metrics instead (ef81fc15).
            has_outcomes = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='session_outcomes'"
            ).fetchone() is not None
            if not has_outcomes:
                rows = conn.execute(
                    "SELECT session_id, started_at, ended_at, "
                    "0 AS duration_s, 0 AS tokens_used, 0 AS files_read, "
                    "0 AS files_modified, 0 AS skills_invoked "
                    "FROM task_sessions WHERE task_id = ? "
                    "ORDER BY started_at ASC",
                    (task_id,),
                ).fetchall()
                return [dict(r) for r in rows]
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

    def task_for_session(self, session_id: str) -> str:
        """Reverse lookup on task_sessions: the most recently started task
        this session is linked to, or '' when unlinked. Backs retrieval
        attribution (who asked) — same mapping consolidation_data reads."""
        if not session_id:
            return ""
        if not self._scores_db:
            for task_id, links in self._session_links.items():
                if session_id in links:
                    return task_id
            return ""
        conn = sqlite_db.connect(self._scores_db, timeout=5.0)
        try:
            self._ensure_task_sessions(conn)
            row = conn.execute(
                "SELECT task_id FROM task_sessions WHERE session_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return row[0] if row else ""
        except sqlite3.OperationalError:
            return ""
        finally:
            conn.close()
