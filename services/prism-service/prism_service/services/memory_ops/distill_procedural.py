"""DistillProceduralOperation — the first concrete MemoryOperation.

Fills the procedural-memory gap: ``memory_type='procedural'`` exists on
``ExpertiseEntry`` but nothing populated it. This op distills recurring
tool/skill sequences out of session history into reusable procedures that
index into Brain like any memory (so they surface in agent context bundles).

The three seams of the MemoryOperation contract:

  * ``select(project)`` — read the real ``scores.db`` ``skill_usage`` +
    ``task_sessions`` tables, cluster sessions that share skills (or a task)
    into recurring-sequence candidates, and enqueue ONE
    ``consolidation_candidate`` per cluster (idempotent on the cluster's
    synthetic session id). Returns the candidate ids — the item the shared
    ``runner.run_one`` looks up.
  * ``build_prompt(item, project)`` — over the cluster's shared-skill /
    member-session signal excerpt, ask Claude "what tool/skill sequence
    recurs and is worth encoding as a reusable procedure?".
  * ``apply_verdict(item, verdict, project)`` — mint each proposed procedure
    as a ``memory_type='procedural'`` entry via ``memory_svc.store``.

Synthesis op -> defaults to Opus per the chassis convention.
"""

from __future__ import annotations

import json as _json
import sqlite3
from pathlib import Path
from typing import Any

from prism_service.services.memory_ops.base import MemoryOperation

#: A cluster must share at least this many skills to be worth distilling.
_MIN_SHARED_SKILLS = 1
#: Cap clusters enqueued per tick so a big history doesn't flood the queue.
_MAX_CLUSTERS = 5


def _scores_db(project: str) -> str | None:
    from prism_service.project_context import get_project
    try:
        ctx = get_project(project)
    except Exception:
        return None
    db = str(ctx._data_dir / "scores.db")
    return db if Path(db).exists() else None


def _cluster_sessions(scores_db: str) -> list[dict]:
    """Group sessions that share skills (and/or a task) into recurring-
    sequence clusters. Each cluster: a key, its member session_ids, the
    shared skill set, and a representative task_id. Deterministic ordering."""
    conn = sqlite3.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        skill_rows = conn.execute(
            "SELECT DISTINCT session_id, skill_name FROM skill_usage"
        ).fetchall()
        task_rows = conn.execute(
            "SELECT task_id, session_id FROM task_sessions"
        ).fetchall()
    finally:
        conn.close()

    # session -> set(skills); session -> task
    by_session: dict[str, set[str]] = {}
    for r in skill_rows:
        by_session.setdefault(r["session_id"], set()).add(r["skill_name"])
    sess_task: dict[str, str] = {r["session_id"]: r["task_id"] for r in task_rows}

    # Cluster by the frozenset of skills a session used: sessions that ran
    # the SAME skill set are the recurring sequence we want to encode.
    clusters: dict[frozenset, list[str]] = {}
    for sid, skills in by_session.items():
        if len(skills) < _MIN_SHARED_SKILLS:
            continue
        clusters.setdefault(frozenset(skills), []).append(sid)

    out: list[dict] = []
    for skills, members in clusters.items():
        if len(members) < 2:
            continue  # a single session is not yet a recurring pattern
        members = sorted(members)
        skills_sorted = sorted(skills)
        task_id = next((sess_task[s] for s in members if s in sess_task), "")
        out.append({
            "key": "|".join(skills_sorted) + "::" + ",".join(members),
            "members": members,
            "skills": skills_sorted,
            "task_id": task_id,
        })
    out.sort(key=lambda c: c["key"])
    return out[:_MAX_CLUSTERS]


class DistillProceduralOperation(MemoryOperation):
    """Distill recurring skill sequences from session history into
    ``memory_type='procedural'`` memories."""

    op_type = "distill_procedural"
    schema = {
        "qualitative_score": "float 0-1",
        "narrative": "string",
        "new_memories": "[{domain, name, description, type, classification}]",
        "invalidate_memory_ids": "[]",
        "confidence": "float 0-1",
    }
    # Synthesis op -> Opus (cheap ops use Haiku); "" falls to CLI default.
    model = "claude-opus-4-20250514"
    provenance = {"source": "distill_procedural", "tier": 2}

    def select(self, project: str) -> list:
        scores_db = _scores_db(project)
        if not scores_db:
            return []
        from prism_service.services.consolidation_data import enqueue_for_session
        items: list[str] = []
        for cluster in _cluster_sessions(scores_db):
            # Synthetic, stable cluster session id so enqueue is idempotent.
            cluster_sid = "distill-proc::" + cluster["key"]
            scope = {
                "cluster": True,
                "member_sessions": cluster["members"],
                "shared_skills": cluster["skills"],
                "task_id": cluster["task_id"],
                "signal_counts": {
                    "pushbacks": 0, "bg_signals": 0,
                    "tool_failures": 0, "memory_writes": 0,
                },
            }
            cid = enqueue_for_session(
                scores_db, cluster_sid, scope=scope,
                trigger="distill_procedural",
            )
            if cid:
                items.append(cid)
            else:
                existing = self._candidate_for_session(scores_db, cluster_sid)
                if existing:
                    items.append(existing)
        return items

    @staticmethod
    def _candidate_for_session(scores_db: str, session_id: str) -> str | None:
        conn = sqlite3.connect(scores_db, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT id FROM consolidation_candidates "
                "WHERE session_id = ? AND status IN ('pending','dispensed') "
                "LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    @staticmethod
    def _load_scope(scores_db: str, item: Any) -> dict:
        conn = sqlite3.connect(scores_db, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT scope_json FROM consolidation_candidates WHERE id = ?",
                (item,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        try:
            return _json.loads(row["scope_json"] or "{}")
        except Exception:
            return {}

    def build_prompt(self, item: Any, project: str) -> str:
        scores_db = _scores_db(project) or ""
        scope = self._load_scope(scores_db, item) if scores_db else {}
        skills = scope.get("shared_skills") or []
        members = scope.get("member_sessions") or []
        skills_line = ", ".join(skills) if skills else "(none)"
        return self._PROMPT.format(
            project=project,
            skills_line=skills_line,
            n_sessions=len(members),
            members=", ".join(members) or "(none)",
        )

    _PROMPT = """You are the PRISM procedural-distillation sub-agent for
project `{project}`. Across {n_sessions} past Claude Code sessions
(ids: {members}) the SAME skill sequence recurred:

  shared skills: {skills_line}

Your job: judge whether this recurring tool/skill sequence is worth
encoding as a REUSABLE PROCEDURE a future agent should follow — e.g.
"when touching sessions.py, run the transcript-import test". A good
procedure is concrete, tied to a trigger (a file / task type), and
captures the ORDER of steps that worked. If the recurrence is
incidental (no durable lesson), return an empty `new_memories` list.

Use Read / Grep / brain_search to ground any file path you cite in the
current checkout. Do not invent steps the sessions did not actually run.

Output ONLY this JSON object on your final turn (no prose, no fence):
- qualitative_score: float 0.0-1.0
- narrative: ~100 words describing the recurring sequence
- new_memories: list of {{domain, name, description, type,
  classification}}. Each entry encodes ONE procedure: `description`
  states the trigger + ordered steps. type='pattern',
  classification in {{tactical, foundational, strategic}}. Empty is fine.
- invalidate_memory_ids: []
- confidence: float 0.0-1.0
"""

    def apply_verdict(self, item: Any, verdict: dict, project: str) -> None:
        from prism_service.project_context import get_project
        ctx = get_project(project)
        for nm in verdict.get("new_memories") or []:
            if not isinstance(nm, dict):
                continue
            required = ("domain", "name", "description", "type", "classification")
            if not all(nm.get(k) for k in required):
                continue
            ctx.memory_svc.store(
                domain=nm["domain"], name=nm["name"],
                description=nm["description"], type=nm["type"],
                classification=nm["classification"],
                memory_type="procedural",
                importance=int(nm.get("importance", 6)),
                evidence={
                    "source": "distill_procedural",
                    "candidate_id": item,
                    "provenance": self.provenance,
                },
            )
