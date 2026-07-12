"""verify_staleness — flag memories that may be confidently WRONG after a refactor.

The named #1 production gap (mem0 2026): a high-relevance memory whose cited
code was rewritten since the memory was learned. We detect this *mechanically*
(no inference) by comparing each memory's ``valid_at`` against the brain.db
``docs.indexed_at`` of the source files in its ``evidence`` — ``indexed_at`` is
re-stamped only when a doc's ``content_hash`` actually changes. A drift =>
enqueue a candidate; the shared runner then asks Claude "is this still true?"
and ``apply_verdict`` keeps / supersedes-with-correction / flags needs_review.
"""

from __future__ import annotations

import json
import sqlite3
from prism_service.services import sqlite_db
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prism_service.services.memory_ops.base import MemoryOperation


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _evidence_files(evidence: Any) -> list[str]:
    """Pull source-file paths out of a memory's evidence dict, tolerant of
    the several shapes evidence takes in the wild (files list / file / dict)."""
    if not isinstance(evidence, dict):
        return []
    out: list[str] = []
    for key in ("files", "file", "sources", "source_files", "paths"):
        val = evidence.get(key)
        if isinstance(val, str):
            out.append(val)
        elif isinstance(val, (list, tuple)):
            out.extend(str(v) for v in val if v)
    return [f for f in out if f]


def _drifted_files(brain_db: str, files: list[str], valid_at: str) -> list[dict]:
    """Return [{path, content, indexed_at}] for cited files whose docs row was
    re-indexed (content_hash changed) AFTER the memory's valid_at."""
    va = _parse_iso(valid_at)
    if va is None or not Path(brain_db).exists():
        return []
    conn = sqlite_db.connect(brain_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    drifted: list[dict] = []
    try:
        for path in files:
            row = conn.execute(
                "SELECT source_file, content, indexed_at FROM docs "
                "WHERE source_file = ? ORDER BY indexed_at DESC LIMIT 1",
                (path,),
            ).fetchone()
            if not row:
                continue
            idx = _parse_iso(row["indexed_at"] or "")
            if idx is not None and idx > va:
                drifted.append({
                    "path": row["source_file"],
                    "content": row["content"] or "",
                    "indexed_at": row["indexed_at"],
                })
    finally:
        conn.close()
    return drifted


class VerifyStalenessOperation(MemoryOperation):
    """Flag + re-judge memories whose cited code drifted past their valid_at."""

    op_type = "verify_staleness"
    schema = {"decision": "str", "confidence": "float", "correction": "str"}
    model = ""  # CLI default (Sonnet) — this is a judgment call over real source
    provenance = {"source": "verify_staleness", "op": "staleness-recheck"}

    # Cap candidates per tick so a big drift never floods one inference sweep.
    LIMIT = 20

    def _ctx(self, project: str):
        from prism_service.project_context import get_project
        return get_project(project)

    def select(self, project: str) -> list:
        ctx = self._ctx(project)
        brain_db = str(ctx._data_dir / "brain.db")
        scores_db = str(ctx._data_dir / "scores.db")
        if not Path(scores_db).exists():
            return []
        mem_svc = ctx.memory_svc
        from prism_service.services.janitor_service import JanitorService
        js = JanitorService(scores_db)
        out: list = []
        for entry in self._active_entries(mem_svc):
            files = _evidence_files(getattr(entry, "evidence", {}))
            if not files:
                continue
            drifted = _drifted_files(brain_db, files, getattr(entry, "valid_at", ""))
            if not drifted:
                continue
            scope = {
                "memory_id": entry.id,
                "domain": entry.domain,
                "drifted_files": [d["path"] for d in drifted],
            }
            out.append(js.enqueue(trigger="verify_staleness", scope=scope))
            if len(out) >= self.LIMIT:
                break
        return out

    @staticmethod
    def _active_entries(mem_svc) -> list:
        """All active, temporally-valid memories across every domain."""
        entries: list = []
        for domain in mem_svc.list_domains():
            for e in mem_svc._read_entries(domain):
                if e.status == "active" and not getattr(e, "invalid_at", ""):
                    entries.append(e)
        return entries

    @staticmethod
    def _scope_for(project: str, item: Any) -> dict:
        from prism_service.project_context import get_project
        ctx = get_project(project)
        scores_db = str(ctx._data_dir / "scores.db")
        conn = sqlite_db.connect(scores_db, timeout=5.0)
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
            return json.loads(row["scope_json"] or "{}")
        except (ValueError, TypeError):
            return {}

    def build_prompt(self, item: Any, project: str) -> str:
        scope = self._scope_for(project, item)
        ctx = self._ctx(project)
        entry = ctx.memory_svc.get_entry(scope.get("memory_id", ""))
        mem_txt = (
            f"name: {entry.name}\ndescription: {entry.description}\n"
            f"learned_at(valid_at): {entry.valid_at}\n"
            f"evidence: {json.dumps(entry.evidence)}"
        ) if entry else "(memory not found)"
        brain_db = str(ctx._data_dir / "brain.db")
        valid_at = entry.valid_at if entry else ""
        drifted = _drifted_files(brain_db, scope.get("drifted_files", []), valid_at)
        src = "\n\n".join(
            f"### {d['path']} (re-indexed {d['indexed_at']})\n{d['content']}"
            for d in drifted
        ) or "(changed source unavailable)"
        return (
            "A stored memory cites source code that was REFACTORED after the "
            "memory was learned. Re-read the now-changed source and judge "
            "whether the memory is STILL TRUE.\n\n"
            f"## Memory\n{mem_txt}\n\n## Changed source (current)\n{src}\n\n"
            "Respond ONLY with JSON carrying BOTH the staleness verdict and "
            "the audit envelope:\n"
            "{\"decision\": \"keep\"|\"supersede\"|\"needs_review\", "
            "\"confidence\": 0.0-1.0, \"correction\": \"<corrected fact if "
            "supersede, else empty>\", \"qualitative_score\": 0.0-1.0, "
            "\"narrative\": \"<one-line rationale>\", \"new_memories\": [], "
            "\"invalidate_memory_ids\": []}. Use \"keep\" if still true, "
            "\"supersede\" if you can state the corrected fact, "
            "\"needs_review\" if uncertain."
        )

    # Below this confidence a 'keep'/'supersede' is downgraded to needs_review.
    UNCERTAIN_BELOW = 0.5

    def apply_verdict(self, item: Any, verdict: dict, project: str) -> None:
        scope = self._scope_for(project, item)
        memory_id = scope.get("memory_id", "")
        if not memory_id:
            return
        mem_svc = self._ctx(project).memory_svc
        entry = mem_svc.get_entry(memory_id)
        if entry is None:
            return
        decision = str(verdict.get("decision", "needs_review")).lower()
        try:
            confidence = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        correction = str(verdict.get("correction", "")).strip()

        if decision == "keep" and confidence >= self.UNCERTAIN_BELOW:
            return  # still true — no-op
        if decision == "supersede" and correction and confidence >= self.UNCERTAIN_BELOW:
            mem_svc.store(
                domain=entry.domain,
                name=entry.name,            # name-match => archives the stale one
                description=correction,
                type=entry.type or "convention",
                classification=entry.classification or "tactical",
                evidence=dict(entry.evidence or {}),
                importance=entry.importance,
                memory_type=entry.memory_type,
            )
            return
        # keep-but-unsure / explicit needs_review / unknown decision => flag.
        mem_svc.update_entry(memory_id, status="needs_review")
