"""Task service — manages tasks in SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
from prism_service.services import sqlite_db
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from prism_service.models.task import (
    DEFAULT_WORKFLOW,
    Task,
    TaskHistory,
    normalize_workflow,
    validate_channel,
    validate_workflow,
)


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


# Open-gate close guard (2026-08-25, live near-miss on task 3baadd19): the
# plain status quick-pill (SPA REST PATCH + MCP task_update alike) let
# status=done through with ZERO awareness of the conductor's own gate
# state -- a single click silently produced a "DONE" task whose green_gate
# had never actually passed, bypassing every distinct-actor/human-only
# safeguard the gate machinery exists to enforce. The ONE real gate-decide
# path (POST /api/conductor/gate, or the Rewind lever to undo a wrong one)
# is unaffected; this only refuses the OTHER, ungated door.
DONE_BLOCKED_BY_OPEN_GATE_FIX = (
    "this task is sitting at an undecided gate ({workflow_step}, "
    "gate_state={gate_state}) — status=done cannot be set directly while "
    "a gate is open. Decide the gate first (POST /api/conductor/gate or "
    "the Evidence tab's Approve/Reject), or use POST /api/conductor/rewind "
    "if it was decided in error, then retry"
)


def is_open_gate_step(workflow_step: str, gate_state: str) -> bool:
    """True when `workflow_step` is a real conductor gate step (per
    models.workflow.WORKFLOW_STEPS) whose decision is not yet settled
    (`gate_state` pending or failed — passed/none/anything else is not
    "open"). Pure and importable from both the REST route and the MCP
    tool so the two guards can never drift apart."""
    from prism_service.models.workflow import WORKFLOW_STEPS

    step = next((s for s in WORKFLOW_STEPS if s["id"] == workflow_step), None)
    return bool(step) and step["type"] == "gate" and gate_state in ("pending", "failed")


# The task page's GET /api/tasks/{id} embeds every TaskHistory row verbatim
# (no pagination — see history() below), so a full-fidelity repr() of a
# changed plan_doc/plan_diagram/description dumps BOTH the entire old and
# entire new text into one row on every edit; a task with several such
# edits ships hundreds of KB of stale text on every page load for no UI
# benefit — PlanView's Timeline only ever shows a short summary until a
# row is manually expanded. Cap the preview here instead: short scalar
# diffs (status/priority/... — always well under the cap) are unaffected,
# so PlanView's `field:'from'->'to'` pill parsing still sees the exact
# values it expects.
_HISTORY_VALUE_PREVIEW_CHARS = 200


def _history_value_repr(value: object, limit: int = _HISTORY_VALUE_PREVIEW_CHARS) -> str:
    """Bounded repr for a history diff entry — full repr for short values,
    a capped preview plus an elided-character count for long ones."""
    if isinstance(value, str) and len(value) > limit:
        elided = len(value) - limit
        return f"{value[:limit]!r}...(+{elided} chars)"
    return repr(value)


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
    plan_diagram TEXT DEFAULT '',
    premise_notes TEXT DEFAULT '',
    channel TEXT DEFAULT '',
    channel_ref TEXT DEFAULT '',
    workflow TEXT DEFAULT ''
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
-- advance_rows_all(): WHERE action='advance_task' ORDER BY task_id,
-- timestamp — without this it full-scans task_history on every
-- phase_progress render (task 9974d407, "PRISM feels instant").
CREATE INDEX IF NOT EXISTS idx_task_history_action_task_ts
    ON task_history(action, task_id, timestamp);
"""


# ── create observers (task 27e543e0) ──────────────────────────────────────
#
# TaskService is the ONE chokepoint every creation path already goes through:
# the REST route (api/tasks.py:214) and the MCP task_create verb
# (mcp/tools.py:4095) both land here. Anything that must happen when a task is
# born therefore belongs on this list rather than being duplicated at each call
# site, where the next new caller would silently miss it.
#
# Deliberately PROVIDER-NEUTRAL: this module must not learn what GitHub is. It
# publishes "a task was created"; services/task_mirror.py decides what that is
# worth. An observer that raises is logged and skipped -- creating a task must
# never fail because a downstream mirror is unhappy.
_CREATE_OBSERVERS: list = []


def add_create_observer(fn) -> bool:
    """Register ``fn(project, task)``. Idempotent; True if newly added."""
    if fn in _CREATE_OBSERVERS:
        return False
    _CREATE_OBSERVERS.append(fn)
    return True


def remove_create_observer(fn) -> bool:
    if fn in _CREATE_OBSERVERS:
        _CREATE_OBSERVERS.remove(fn)
        return True
    return False


def create_observers() -> list:
    """Point-in-time copy — lets a caller ASSERT what production registered."""
    return list(_CREATE_OBSERVERS)


# Status observers (task 0a9b511f). The SAME chokepoint argument as creation:
# every caller that moves a task's status -- the REST route, the MCP
# task_update verb, and the conductor -- goes through TaskService.update, so a
# listener here cannot be bypassed by a caller that forgot to opt in.
#
# This existed for creation and NOT for status, which is exactly why the mirror
# could open a GitHub issue but never close one: push_task_closure was written
# and tested, and nothing but a hand-POSTed endpoint could reach it.
#
# Provider-neutral for the same reason as above: this module publishes "a task's
# status changed", and task_mirror decides what that is worth.
_STATUS_OBSERVERS: list = []


def add_status_observer(fn) -> bool:
    """Register ``fn(project, task, old_status)``. Idempotent; True if added.

    ``old_status`` is passed rather than left for the observer to look up: by
    the time it runs the row already holds the new value, so the previous one is
    otherwise unrecoverable.
    """
    if fn in _STATUS_OBSERVERS:
        return False
    _STATUS_OBSERVERS.append(fn)
    return True


def remove_status_observer(fn) -> bool:
    if fn in _STATUS_OBSERVERS:
        _STATUS_OBSERVERS.remove(fn)
        return True
    return False


def status_observers() -> list:
    """Point-in-time copy — lets a caller ASSERT what production registered."""
    return list(_STATUS_OBSERVERS)




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
    # premise_notes (task 3928b7ac): review_previous_notes' own dedicated
    # field, decoupled from the shared completion_proof column above.
    ("premise_notes", "TEXT DEFAULT ''"),
    # Channel provenance (task b480eb15): which entry point created the
    # task + an opaque origin ref. Existing rows backfill to '' (legacy).
    ("channel", "TEXT DEFAULT ''"),
    ("channel_ref", "TEXT DEFAULT ''"),
    # Workflow provenance (task af396b2c): which PRISM workflow drives the
    # task. Existing rows backfill to '' -- read as DEFAULT_WORKFLOW via
    # normalize_workflow at hydration time (_row_to_task), never blank.
    ("workflow", "TEXT DEFAULT ''"),
]


class TaskService:
    """Manages the tasks.db lifecycle and CRUD operations."""

    def __init__(
        self, db_path: str, embed_fn: Optional[EmbedFn] = None,
        scores_db: Optional[str] = None, project: str = "",
    ) -> None:
        # Which project this store belongs to. Carried so a create observer
        # (task 27e543e0) can name the project it was told about — every
        # downstream lookup is project-scoped, and a service that cannot say
        # which project it is cannot hand that on.
        self.project = project
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
        # Event-invalidated snapshot of advance_rows_all(): the conductor's
        # step-duration stats (_median_step_s + _per_step_typical) both call
        # it on EVERY phase_progress render, and the underlying rows only
        # change on an advance_task transition — so serve the same grouped
        # dict until _record_history sees one (task 9974d407).
        self._advance_rows_cache: Optional[dict] = None

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
            premise_notes=(row["premise_notes"] if "premise_notes" in keys
                           and row["premise_notes"] is not None else ""),
            channel=(row["channel"] if "channel" in keys
                     and row["channel"] is not None else ""),
            channel_ref=(row["channel_ref"] if "channel_ref" in keys
                         and row["channel_ref"] is not None else ""),
            workflow=normalize_workflow(
                row["workflow"] if "workflow" in keys
                and row["workflow"] is not None else ""),
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
        if action == "advance_task":
            # New advance row → the cached advance_rows_all snapshot is stale.
            self._advance_rows_cache = None

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
        premise_notes: str = "",
        channel: str = "",
        channel_ref: str = "",
        workflow: str = "",
    ) -> Task:
        """Create a new task and return it. Raises ValueError for a channel
        outside models.task.CHANNELS (blank is allowed — legacy), or a
        workflow outside models.task.WORKFLOW_ALIASES (blank resolves to
        DEFAULT_WORKFLOW so every newly-created task names a real driver)."""
        channel = validate_channel(channel)
        workflow = validate_workflow(workflow) or DEFAULT_WORKFLOW
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
            premise_notes=premise_notes,
            channel=channel,
            channel_ref=channel_ref or "",
            workflow=workflow,
        )
        self._db.execute(
            "INSERT INTO tasks "
            "(id, title, description, status, priority, story_file, "
            "assigned_agent, created_at, updated_at, completed_at, "
            "blocked_reason, dependencies, tags, parent_id, "
            "oracle, proof_type, completion_proof, likely_misfire, "
            "full_outcome_complete, "
            "allowed_files, verify, stop_if, plan_doc, plan_diagram, "
            "premise_notes, channel, channel_ref, workflow) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                task.premise_notes,
                task.channel,
                task.channel_ref,
                task.workflow,
            ),
        )
        self._db.commit()
        self._record_history(task.id, "created", f"title={title!r}")
        # LL-03: embed title+description so LL-06's similarity retrieval
        # has something to search over. Silent on embedder-offline —
        # the row still exists, just without a vector.
        self._store_embedding(task.id, task.title, task.description)
        self._notify_created(task)
        return task

    def _notify_created(self, task: Task) -> None:
        """Publish "a task was created" to every registered observer.

        AFTER the commit, so an observer always sees a row that really exists,
        and each observer is isolated: a failing mirror must never turn a
        successful task creation into an error the user sees.
        """
        for observer in list(_CREATE_OBSERVERS):
            try:
                observer(self.project, task)
            except Exception:
                logging.getLogger(__name__).warning(
                    "task create observer %r failed for %s",
                    getattr(observer, "__name__", observer), task.id,
                    exc_info=True)

    def _notify_status(self, task: Task, old_status: str) -> None:
        """Publish "a task's status changed" to every registered observer.

        Same contract as _notify_created and for the same reasons: AFTER the
        commit, so an observer never sees a status the database does not hold,
        and isolated per observer, so a provider outage cannot turn a
        successful task_update into an error the user sees.
        """
        for observer in list(_STATUS_OBSERVERS):
            try:
                observer(self.project, task, old_status)
            except Exception:
                logging.getLogger(__name__).warning(
                    "task status observer %r failed for %s",
                    getattr(observer, "__name__", observer), task.id,
                    exc_info=True)

    def ensure_external_intake(
        self,
        task_id: str,
        title: str,
        description: str = "",
        tags: Optional[list[str]] = None,
        priority: int = 0,
        channel: str = "",
        channel_ref: str = "",
    ) -> Optional[Task]:
        """Idempotently materialize a LOCAL pending intake task for an imported
        external work item (task fddfd75a).

        The id is deterministic (UUIDv5 of the canonical provider tuple), so a
        repeated import or a crash-retry converges on ONE row. If the task
        already exists it is returned UNCHANGED — a later pull must never
        clobber a user-edited local title/status/assignment/workflow. The row
        is created ``pending`` with an empty workflow_step and gate_state='none'
        and NO linked session: remote status never enters the conductor.
        """
        existing = self.get(task_id)
        if existing is not None:
            return existing
        channel = validate_channel(channel)
        now = datetime.now(timezone.utc).isoformat()
        # INSERT OR IGNORE (not a bare INSERT) so two concurrent same-id pulls
        # converge on one row instead of racing to an IntegrityError.
        self._db.execute(
            "INSERT OR IGNORE INTO tasks "
            "(id, title, description, status, priority, created_at, tags, "
            "workflow_step, gate_state, channel, channel_ref) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, '', 'none', ?, ?)",
            (task_id, title, description, priority, now, json.dumps(tags or []),
             channel, channel_ref or ""),
        )
        self._db.commit()
        self._record_history(task_id, "external_intake", f"title={title!r}")
        return self.get(task_id)

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

    def active_ids(self) -> list[str]:
        """Ids of tasks currently in motion — status in_progress, or a live
        workflow_step on a non-terminal row. ONE slim indexed query for the
        pollers (work_stream ticker) that previously walked the conductor's
        full managed_tasks() board — phase_progress, histories, sessions and
        all — every 1.5s just to learn which ids to look at (task 9974d407)."""
        rows = self._db.execute(
            "SELECT id FROM tasks WHERE status = 'in_progress' "
            "OR (workflow_step <> '' "
            "    AND status NOT IN ('done', 'cancelled', 'deleted'))"
        ).fetchall()
        return [r["id"] for r in rows]

    def update(self, task_id: str, **kwargs: object) -> Optional[Task]:
        """Update arbitrary fields on a task. Records change history."""
        task = self.get(task_id)
        if task is None:
            return None

        # Captured BEFORE the mutation loop below, which writes the new value
        # onto this same object (task 0a9b511f). Observers need the previous
        # status to tell a real transition from a no-op re-write.
        old_status = task.status

        now = datetime.now(timezone.utc).isoformat()
        changes: list[str] = []
        # D-1: only SCALAR changed fields ride the push event — never the
        # full row (lists/markdown blobs stay off the wire; the client
        # already holds those and doesn't need them re-pushed).
        changed_fields: dict[str, object] = {}

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
            changes.append(
                f"{key}: {_history_value_repr(old_value)} -> {_history_value_repr(value)}"
            )
            if value is None or isinstance(value, (str, int, float, bool)):
                changed_fields[key] = value

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
            "plan_doc=?, plan_diagram=?, premise_notes=?, "
            "channel=?, channel_ref=?, workflow=? "
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
                task.premise_notes,
                task.channel,
                task.channel_ref,
                task.workflow,
                task.id,
            ),
        )
        self._db.commit()
        self._record_history(task.id, "updated", "; ".join(changes))
        # D-1/D-2: push a lean task.changed event so /sse/tasks can deliver
        # it — the write already committed above, so a publish failure
        # (bus down, serialization) must never surface as a write failure.
        try:
            from prism_service.events import bus

            bus.publish({
                "project": self.project,
                "type": "task.changed",
                "task_id": task.id,
                # gamify round6 item 2 (atomic card+wire): carry the task's
                # REAL parent_id on every event, not just when parent_id
                # itself was the field that changed -- the /live SPA can
                # only ever derive a subtask's parent_of edge at the same
                # instant its card is born if the very event that births
                # it already names the parent (empty string = a real
                # root, no edge expected).
                "parent_id": task.parent_id or "",
                "fields": changed_fields,
                "updated_at": task.updated_at,
            })
        except Exception:
            logging.getLogger(__name__).warning(
                "task.changed publish failed for %s", task.id, exc_info=True)
        # Wake task_runner's background loop on the SAME event, so a task
        # that just became eligible (status -> in_progress, or a step/gate
        # transition) gets swept immediately instead of waiting out
        # PRISM_TASK_RUNNER_INTERVAL -- observed live sitting idle ~16min
        # as the ONLY eligible task in the project before this existed.
        # wake() is a no-op-safe flag set; never raises, never blocks.
        try:
            from prism_service.services import task_runner

            task_runner.wake()
        except Exception:
            logging.getLogger(__name__).warning(
                "task_runner.wake() failed for %s", task.id, exc_info=True)
        # LL-03: re-embed only when the title or description changed.
        # Priority / status / tag-only updates don't move the vector.
        if "title" in kwargs or "description" in kwargs:
            self._store_embedding(task.id, task.title, task.description)
        # Publish the transition LAST, after the row is committed and its audit
        # row written, and ONLY when the status actually moved (task 0a9b511f).
        # The `changes` early-return above already drops no-op writes; this
        # second guard is what keeps a title or priority edit from reaching a
        # provider at all, rather than relying on the observer to filter.
        if task.status != old_status:
            self._notify_status(task, old_status)
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

    def advance_rows_all(self) -> dict[str, list[tuple[str, str]]]:
        """Every `advance_task` history row in the project, grouped by task id
        as (timestamp, details) ordered ascending.

        ONE query, for the project-wide step-duration statistics the conductor
        computes on every task-page render (_median_step_s, _per_step_typical).
        Those walked `list()` and then called `history(t.id)` PER TASK — 458
        extra queries per render, ~470 SQL round trips to answer a question
        that does not even depend on which task is being viewed. The filter
        rides in SQL so the rows that never mattered are never deserialized.
        """
        cached = self._advance_rows_cache
        if cached is not None:
            return cached
        rows = self._db.execute(
            "SELECT task_id, timestamp, details FROM task_history "
            "WHERE action = 'advance_task' ORDER BY task_id, timestamp ASC"
        ).fetchall()
        out: dict[str, list[tuple[str, str]]] = {}
        for r in rows:
            out.setdefault(r["task_id"], []).append(
                (r["timestamp"] or "", r["details"] or ""))
        self._advance_rows_cache = out
        return out

    # ------------------------------------------------------------------
    # Task <-> session association (LL — activates task_sessions)
    # ------------------------------------------------------------------

    def _scores_conn_cached(self) -> sqlite3.Connection:
        """The calling thread's connection to scores.db — same thread-local
        pattern as ``self._db`` for tasks.db. sessions_for_task used to open
        a fresh connection (plus CREATE TABLE DDL) on EVERY call, and it is
        invoked once per tile per board render; the connect+DDL tax was pure
        overhead (task 9974d407). task_sessions DDL runs once per connection.
        Callers must NOT close the returned connection."""
        conn = getattr(self._tlocal, "scores_conn", None)
        if conn is None:
            conn = sqlite_db.connect(self._scores_db, timeout=5.0)
            self._ensure_task_sessions(conn)
            self._tlocal.scores_conn = conn
        return conn

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
        conn = self._scores_conn_cached()
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
        return True

    def sessions_for_task(self, task_id: str) -> list[dict]:
        """Return the sessions linked to `task_id`, each row LEFT JOINed
        against session_outcomes so the UI gets per-session timing +
        token + file + skill metrics in one shape:
          [{session_id, started_at, ended_at, duration_s, tokens_used,
            files_read, files_modified, skills_invoked, shared_across_tasks}]
        Empty list when nothing is linked (backs the UI empty state).

        CROSS-TASK TOKEN BLEED (mx-7677c8, task 3baadd19 qa discovery,
        2026-08-24): session_outcomes (brain_engine.py) is keyed ONLY by
        session_id -- one GLOBAL lifetime-totals row per session, no
        task_id column at all. A session that legitimately touches more
        than one task (task_sessions IS correctly per-task) therefore
        showed the exact same duration/tokens/files numbers on EVERY task
        it ever linked to -- reproduced live: session 5a315ea3 showed
        byte-identical duration_s=7821/tokens_used=6814662 on both task
        3baadd19 (linked 2026-08-23) and an unrelated new epic (linked
        2026-08-24). `shared_across_tasks` marks this honestly: True when
        this session_id appears in task_sessions under more than one
        task_id, in which case the metrics are ZEROED rather than
        presenting the session's all-time totals as if they were this
        task's own -- a session_outcomes row simply cannot answer "how
        much of this session's activity happened on THIS task" (no
        per-task attribution is recorded at write time), so the honest
        move is to say nothing rather than repeat a number that is
        provably wrong on at least one of the tasks it's shown on.
        """
        if not self._scores_db:
            return [
                {"session_id": r["session_id"], "started_at": r["started_at"],
                 "ended_at": r["ended_at"], "duration_s": 0, "tokens_used": 0,
                 "files_read": 0, "files_modified": 0, "skills_invoked": 0,
                 "shared_across_tasks": False}
                for r in self._session_links.get(task_id, {}).values()
            ]
        conn = self._scores_conn_cached()
        try:
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
                result = [dict(r) for r in rows]
                for r in result:
                    r["shared_across_tasks"] = False
                return result
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
            result = [dict(r) for r in rows]
            if result:
                session_ids = [r["session_id"] for r in result]
                placeholders = ",".join("?" * len(session_ids))
                shared = {
                    row[0] for row in conn.execute(
                        "SELECT session_id FROM task_sessions "
                        f"WHERE session_id IN ({placeholders}) "
                        "GROUP BY session_id HAVING COUNT(DISTINCT task_id) > 1",
                        session_ids,
                    ).fetchall()
                }
                for r in result:
                    r["shared_across_tasks"] = r["session_id"] in shared
                    if r["shared_across_tasks"]:
                        r["duration_s"] = 0
                        r["tokens_used"] = 0
                        r["files_read"] = 0
                        r["files_modified"] = 0
                        r["skills_invoked"] = 0
            return result
        except sqlite3.OperationalError:
            return []

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
        try:
            conn = self._scores_conn_cached()
            row = conn.execute(
                "SELECT task_id FROM task_sessions WHERE session_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return row[0] if row else ""
        except sqlite3.OperationalError:
            return ""
