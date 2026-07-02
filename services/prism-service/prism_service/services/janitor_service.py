"""JanitorService — queue, readiness policy, retry/backoff for the
Layer-B caller-side reflection loop.

Parent task: 37932f3f · LL-07.

PRISM runs zero LLMs. This service schedules consolidation work; the
actual LLM compute happens in the caller's Claude session via a sub-agent
spawned from the brief returned by :meth:`check`. Results come back via
:meth:`submit` (success) or :meth:`abandon` (retry or give up).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional


# Readiness / backoff constants. Small, exported as class attributes so
# tests can monkeypatch without reaching into the module.
_MIN_QUEUE_AGE_S = 3600          # 1 hour — git-truth signals settle
_ENQUEUE_DEBOUNCE_S = 600        # 10 min — idempotency window
_ABANDON_BACKOFF_S = 300         # 5 min — don't redispense immediately
_HARD_RETRY_LIMIT = 3            # 3 strikes → abandoned

# Default MCP allow-list for the prism-reflect sub-agent. Read-only
# tools only; the caller's Claude spawns the sub-agent with these.
_DEFAULT_MCPS: tuple[str, ...] = (
    "brain_search",
    "brain_graph",
    "brain_find_symbol",
    "brain_find_references",
    "brain_call_chain",
    "memory_recall",
    "task_list",
)

# The response schema the sub-agent must produce. Server validates on
# submit; malformed output is rejected and the candidate stays queued.
_RESPONSE_SCHEMA: dict[str, str] = {
    "qualitative_score": "float 0-1",
    "narrative": "string ~200 words",
    "new_memories": "[{domain, name, description, type, classification}]",
    "invalidate_memory_ids": "[{id, reason}]",
    "confidence": "float 0-1",
}


# Task 1a7bc848 — candidate classification. consolidation_candidates is a
# SHARED queue: the memory-ops runner (memory_ops/runner.py) enqueues its
# own work items (merge / forget / verify_staleness / distill_procedural)
# with task_id=NULL and op-shaped scopes. Those are NOT reflection briefs;
# dispensing one via check() renders the empty "task None" brief.
_RUNNER_OWNED_TRIGGERS: frozenset = frozenset({
    "merge", "forget", "verify_staleness", "distill_procedural",
})


def is_runner_owned(trigger: Optional[str], scope: dict) -> bool:
    """True when the candidate is a memory-ops runner work item — owned
    by memory_ops/runner.py, never dispensable as a reflection brief."""
    return (trigger in _RUNNER_OWNED_TRIGGERS) or bool(scope.get("op"))


def scope_has_reflection_context(scope: dict) -> bool:
    """True when the scope carries something a reflection sub-agent can
    actually investigate — the exact fields _build_brief surfaces:
    file_paths / memory_ids / task_ids / transcript_excerpt, or any
    nonzero signal_counts bucket."""
    for key in ("file_paths", "memory_ids", "task_ids"):
        if scope.get(key):
            return True
    if (scope.get("transcript_excerpt") or "").strip():
        return True
    # v6.2.18 contract (task ed7292bd, test_consolidation_noise_filter):
    # a session that MODIFIED files is signal even with empty buckets.
    try:
        if int(scope.get("files_modified", 0) or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    counts = scope.get("signal_counts") or {}
    if isinstance(counts, dict):
        for v in counts.values():
            try:
                if int(v or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _overlap(a: dict, b: dict) -> bool:
    """Two scope dicts overlap if any shared list-valued key has a
    non-empty intersection. Keys we care about: task_ids, memory_ids,
    file_paths."""
    for key in ("task_ids", "memory_ids", "file_paths"):
        aset = set(a.get(key) or [])
        bset = set(b.get(key) or [])
        if aset & bset:
            return True
    return False


class JanitorService:
    """Queue + dispensing + retry policy for Layer-B reflection."""

    # Exposed so tests can reach in without magic-number duplication.
    MIN_QUEUE_AGE_S = _MIN_QUEUE_AGE_S
    ENQUEUE_DEBOUNCE_S = _ENQUEUE_DEBOUNCE_S
    ABANDON_BACKOFF_S = _ABANDON_BACKOFF_S
    HARD_RETRY_LIMIT = _HARD_RETRY_LIMIT

    def __init__(
        self,
        scores_db: str,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._db_path = scores_db
        self._db = sqlite3.connect(scores_db, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._clock: Callable[[], datetime] = clock or _now_utc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _iso(self, dt: Optional[datetime] = None) -> str:
        return (dt or self._clock()).isoformat()

    def _parse_iso(self, s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    def _scope_of(self, row: sqlite3.Row) -> dict:
        try:
            return json.loads(row["scope_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    def _task_resolves(self, task_id: Optional[str]) -> bool:
        """True when ``task_id`` names a real row in the sibling tasks.db.

        Guards the likely_misfire: a non-null-but-garbage task_id must
        not carry an otherwise-empty brief through the dispense gate.
        When tasks.db is absent (standalone scores.db fixtures) a
        non-null id is trusted — we cannot verify, and dropping real
        work would be worse."""
        if not task_id:
            return False
        tasks_db = Path(self._db_path).parent / "tasks.db"
        if not tasks_db.exists():
            return True
        try:
            conn = sqlite3.connect(str(tasks_db))
            try:
                row = conn.execute(
                    "SELECT 1 FROM tasks WHERE id=? LIMIT 1", (task_id,)
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return True  # tasks table absent/locked — never drop real work
        return row is not None

    def _has_reflection_grounding(
        self, task_id: Optional[str], scope: dict,
    ) -> bool:
        """A brief is actionable iff it has investigable scope context or
        a task_id that resolves to a real task."""
        return scope_has_reflection_context(scope) or self._task_resolves(task_id)

    # ------------------------------------------------------------------
    # enqueue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        trigger: str = "manual",
        scope: Optional[dict] = None,
    ) -> str:
        """Insert a new candidate. Idempotent on (task_id, trigger) within
        a 10-minute window — returns the existing id instead of inserting
        a duplicate."""
        now = self._clock()
        if task_id is not None:
            cutoff = (now - timedelta(seconds=self.ENQUEUE_DEBOUNCE_S)).isoformat()
            existing = self._db.execute(
                "SELECT id FROM consolidation_candidates "
                "WHERE task_id=? AND trigger=? AND status='pending' "
                "AND queued_at > ? "
                "ORDER BY queued_at DESC LIMIT 1",
                (task_id, trigger, cutoff),
            ).fetchone()
            if existing:
                return existing["id"]
        cid = str(uuid.uuid4())
        self._db.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, scope_json, status, queued_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (
                cid, task_id, session_id, trigger,
                json.dumps(scope or {}),
                self._iso(now),
            ),
        )
        self._db.commit()
        return cid

    # ------------------------------------------------------------------
    # mark_stale
    # ------------------------------------------------------------------

    def mark_stale(self, session_id: str, scope: Optional[dict] = None) -> list[str]:
        """Flip pending candidates whose scope overlaps to 'stale' and
        requeue a fresh sibling. Completed candidates are preserved.

        Returns the list of candidate ids that were staled."""
        if not scope:
            return []
        now = self._clock()
        pending = self._db.execute(
            "SELECT id, task_id, trigger, scope_json FROM consolidation_candidates "
            "WHERE status='pending'"
        ).fetchall()
        staled: list[str] = []
        for row in pending:
            if _overlap(self._scope_of(row), scope):
                staled.append(row["id"])
        if not staled:
            return staled

        placeholders = ",".join("?" * len(staled))
        self._db.execute(
            f"UPDATE consolidation_candidates "
            f"SET status='stale', staled_at=? "
            f"WHERE id IN ({placeholders})",
            (self._iso(now), *staled),
        )
        # Requeue a fresh copy for each staled candidate, preserving scope.
        for row in pending:
            if row["id"] not in staled:
                continue
            fresh_id = str(uuid.uuid4())
            self._db.execute(
                "INSERT INTO consolidation_candidates "
                "(id, task_id, session_id, trigger, scope_json, status, queued_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    fresh_id, row["task_id"], session_id, row["trigger"],
                    row["scope_json"], self._iso(now),
                ),
            )
        self._db.commit()
        return staled

    # ------------------------------------------------------------------
    # check — dispense a ready brief
    # ------------------------------------------------------------------

    def check(self, session_id: str) -> dict:
        """Return {ready, brief}. Dispenses max one candidate per call.

        Task 1a7bc848 — dispense gate. Runner-owned memory-op rows are
        skipped (left pending for memory_ops/runner.py), and rows with
        neither a resolvable task_id nor usable scope context are
        terminally retired (status='abandoned') so the queue DRAINS
        instead of cycling un-actionable "task None" briefs through
        abandon-backoff forever."""
        now = self._clock()
        age_cutoff = (now - timedelta(seconds=self.MIN_QUEUE_AGE_S)).isoformat()
        backoff_cutoff = (now - timedelta(seconds=self.ABANDON_BACKOFF_S)).isoformat()
        rows = self._db.execute(
            "SELECT * FROM consolidation_candidates "
            "WHERE status='pending' "
            "  AND queued_at <= ? "
            "  AND (dispensed_at IS NULL OR dispensed_at <= ?) "
            "ORDER BY queued_at ASC",
            (age_cutoff, backoff_cutoff),
        ).fetchall()

        chosen: Optional[sqlite3.Row] = None
        retired: list[str] = []
        for row in rows:
            scope = self._scope_of(row)
            if is_runner_owned(row["trigger"], scope):
                continue  # memory-ops work item — not a reflection brief
            if not self._has_reflection_grounding(row["task_id"], scope):
                retired.append(row["id"])
                continue
            chosen = row
            break

        if retired:
            placeholders = ",".join("?" * len(retired))
            self._db.execute(
                f"UPDATE consolidation_candidates SET status='abandoned' "
                f"WHERE id IN ({placeholders})",
                retired,
            )
            self._db.commit()

        if chosen is None:
            return {"ready": False, "brief": None}

        # Claim it
        self._db.execute(
            "UPDATE consolidation_candidates "
            "SET status='dispensed', dispensed_at=? WHERE id=?",
            (self._iso(now), chosen["id"]),
        )
        self._db.commit()

        brief = self._build_brief(chosen)
        return {"ready": True, "brief": brief}

    def next_pending_for_nudge(self, nudge_cutoff_iso: str) -> Optional[tuple]:
        """Oldest pending, not-recently-nudged, DISPENSABLE reflection
        candidate — the only kind worth nagging an agent about (task
        1a7bc848: runner-owned and null-context rows must never emit
        PRISM_REFLECTION_PENDING). Stamps last_nudged_at on the returned
        row. Returns (candidate_id, task_id_or_empty) or None."""
        rows = self._db.execute(
            "SELECT * FROM consolidation_candidates "
            "WHERE status='pending' "
            "  AND (last_nudged_at IS NULL OR last_nudged_at <= ?) "
            "ORDER BY queued_at ASC",
            (nudge_cutoff_iso,),
        ).fetchall()
        for row in rows:
            scope = self._scope_of(row)
            if is_runner_owned(row["trigger"], scope):
                continue
            if not self._has_reflection_grounding(row["task_id"], scope):
                continue
            self._db.execute(
                "UPDATE consolidation_candidates SET last_nudged_at=? "
                "WHERE id=?",
                (self._iso(), row["id"]),
            )
            self._db.commit()
            return row["id"], row["task_id"] or ""
        return None

    def _build_brief(self, row: sqlite3.Row) -> dict:
        scope = self._scope_of(row)
        task_id = row["task_id"]
        # Pull quantitative score if Layer-A already scored the task.
        quantitative_score: Optional[float] = None
        try:
            qrow = self._db.execute(
                "SELECT quality_score, cuped_score FROM task_quality_rollup "
                "WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if qrow is not None:
                quantitative_score = qrow["cuped_score"] or qrow["quality_score"]
        except sqlite3.OperationalError:
            pass

        # Scope is mostly trusted metadata (id lists, file paths). The
        # transcript_excerpt, if present, is user-sourced and gets
        # wrapped so the sub-agent treats it as data, not instructions.
        transcript = scope.get("transcript_excerpt", "")
        wrapped_transcript = (
            f"<untrusted>{transcript}</untrusted>" if transcript else ""
        )

        context = {
            "task_id": task_id,
            "affected_files": scope.get("file_paths", []),
            "affected_memory_ids": scope.get("memory_ids", []),
            "affected_task_ids": scope.get("task_ids", []),
            "quantitative_score": quantitative_score,
            "transcript_excerpt": wrapped_transcript,
        }
        guidance_parts = []
        if context["affected_files"]:
            guidance_parts.append(
                f"Use brain_graph / brain_call_chain to trace callers of "
                f"methods in {context['affected_files']}."
            )
        if context["affected_memory_ids"]:
            guidance_parts.append(
                "Use memory_recall to check whether the affected memories "
                "still apply given the current code."
            )
        guidance_parts.append(
            "Use brain_search to find similar past patterns across tasks."
        )
        return {
            "candidate_id": row["id"],
            "question": (
                f"Did the approach taken on task {task_id} produce "
                f"durable, well-integrated code?"
            ),
            "context": context,
            "mcps_available": list(_DEFAULT_MCPS),
            "investigation_guidance": " ".join(guidance_parts),
            "response_schema": dict(_RESPONSE_SCHEMA),
        }

    # ------------------------------------------------------------------
    # submit
    # ------------------------------------------------------------------

    _REQUIRED_FIELDS = (
        "qualitative_score", "narrative", "new_memories",
        "invalidate_memory_ids", "confidence",
    )

    def submit(
        self,
        candidate_id: str,
        output_json: dict,
        op_type: str = "reflection",
    ) -> dict:
        """Validate, write consolidation_runs, enrich rollup, store
        new memories, invalidate old ones. Returns {accepted, error?}.

        ``op_type`` names the memory-operation family (reflection / forget /
        prune / distill / ...) so every verdict row is replayable + diffable.
        """
        if not isinstance(output_json, dict):
            return {"accepted": False, "error": "output_json must be a dict"}
        missing = [f for f in self._REQUIRED_FIELDS if f not in output_json]
        if missing:
            return {
                "accepted": False,
                "error": f"missing required fields: {missing}",
            }
        row = self._db.execute(
            "SELECT task_id FROM consolidation_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return {"accepted": False, "error": "unknown candidate_id"}
        now = self._clock()
        run_id = str(uuid.uuid4())
        self._db.execute(
            "INSERT INTO consolidation_runs "
            "(id, candidate_id, run_at, output_json, subagent_type, "
            " confidence, schema_valid, op_type) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (
                run_id, candidate_id, self._iso(now),
                json.dumps(output_json), "prism-reflect",
                float(output_json.get("confidence") or 0.0),
                op_type,
            ),
        )
        # Enrich rollup — upsert qualitative_score onto the task.
        task_id = row["task_id"]
        if task_id:
            qscore = float(output_json["qualitative_score"])
            self._db.execute(
                "INSERT INTO task_quality_rollup (task_id, qualitative_score) "
                "VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET qualitative_score=excluded.qualitative_score",
                (task_id, qscore),
            )
        # Mark candidate complete.
        self._db.execute(
            "UPDATE consolidation_candidates "
            "SET status='completed', completed_at=? WHERE id=?",
            (self._iso(now), candidate_id),
        )
        self._db.commit()
        # Memory store/invalidate are delegated to the caller (MCP layer
        # wires them up in LL-08). We expose the counts so observability
        # tests can assert the structure; actual writes happen upstream.
        return {
            "accepted": True,
            "run_id": run_id,
            "memories_to_store": len(output_json.get("new_memories") or []),
            "memories_to_invalidate": len(
                output_json.get("invalidate_memory_ids") or []
            ),
        }

    # ------------------------------------------------------------------
    # abandon
    # ------------------------------------------------------------------

    def abandon(self, candidate_id: str, reason: str = "") -> dict:
        """Increment retry_count; status stays 'pending' (subject to
        backoff) until HARD_RETRY_LIMIT is hit."""
        row = self._db.execute(
            "SELECT retry_count FROM consolidation_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return {"accepted": False, "error": "unknown candidate_id"}
        new_count = (row["retry_count"] or 0) + 1
        status = "abandoned" if new_count >= self.HARD_RETRY_LIMIT else "pending"
        self._db.execute(
            "UPDATE consolidation_candidates "
            "SET status=?, retry_count=? WHERE id=?",
            (status, new_count, candidate_id),
        )
        self._db.commit()
        return {"accepted": True, "status": status, "retry_count": new_count}
