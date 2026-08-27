"""knowledge_health -- the Knowledge health scoreboard (task b1971944,
epic 61821448).

Cheap, read-only metrics over stores every project already has: brain.db
(searches/search_feedback), Understand's own memory rows, graph.db's own
entities, and the ontology's own rule catalog and open Queue signals.
Nothing here writes anything; metrics(project) is safe to call as often
as the Workflows page polls, backed by a 60-second per-project cache so a
poll never re-scans sqlite on every tick.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from prism_service.config import project_data_dir
from prism_service.services import sqlite_db

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[float, dict]] = {}


def metrics(project: str) -> dict:
    """The Knowledge health scoreboard for `project`, cached for
    _CACHE_TTL_S seconds."""
    now = time.monotonic()
    cached = _cache.get(project)
    if cached is not None and now - cached[0] < _CACHE_TTL_S:
        return cached[1]
    result = _compute(project)
    _cache[project] = (now, result)
    return result


def _open(path: Path) -> Optional[sqlite3.Connection]:
    """Open `path` through the ONE sqlite chokepoint (services/sqlite_db.py,
    task dde1162f) -- never a bare, unrouted connect call. Read-only for
    this module's own purposes, but the same funnel applies regardless."""
    if not path.exists():
        return None
    try:
        return sqlite_db.connect(path)
    except Exception:
        logger.warning("could not open %s for knowledge health", path,
                        exc_info=True)
        return None


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _search_rates(project: str) -> tuple[float, float]:
    """(search_feedback_rate, recall_to_use_rate) off brain.db's own
    searches/search_feedback tables. search_feedback_rate is the share of
    all searches that got ANY feedback row; recall_to_use_rate narrows
    that to memory-domain searches whose feedback signal is "used"."""
    conn = _open(project_data_dir(project) / "brain.db")
    if conn is None:
        return 0.0, 0.0
    try:
        total = _count(conn, "SELECT COUNT(*) FROM searches")
        with_feedback = _count(
            conn, "SELECT COUNT(DISTINCT search_id) FROM search_feedback")
        feedback_rate = round(with_feedback / total, 4) if total else 0.0

        memory_searches = _count(
            conn,
            "SELECT COUNT(*) FROM searches "
            "WHERE domain='memory' OR domains LIKE '%memory%'")
        memory_used = _count(
            conn,
            "SELECT COUNT(DISTINCT f.search_id) FROM search_feedback f "
            "JOIN searches s ON s.id = f.search_id "
            "WHERE (s.domain='memory' OR s.domains LIKE '%memory%') "
            "AND f.signal='used'")
        use_rate = round(memory_used / memory_searches, 4) if memory_searches else 0.0
        return feedback_rate, use_rate
    finally:
        conn.close()


def _module_files(project: str) -> set[str]:
    """Every distinct file path graph.db's own entities table has seen --
    "a module has code the graph knows about"."""
    conn = _open(project_data_dir(project) / "graph.db")
    if conn is None:
        return set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT file FROM entities WHERE file IS NOT NULL AND file != ''"
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()
    finally:
        conn.close()


def _memory_metrics(project: str, module_files: set[str]) -> tuple[float, float, int, int]:
    """(median_memory_chars, evidence_ratio, concepts_grounded_in_code,
    modules_with_knowledge) off Understand's own active memory rows."""
    try:
        from prism_service.project_context import get_project

        memory_svc = get_project(project).memory_svc
        entries = []
        for domain in memory_svc.list_domains():
            entries.extend(memory_svc.list_entries(domain))
    except Exception:
        return 0.0, 0.0, 0, 0

    if not entries:
        return 0.0, 0.0, 0, 0

    lengths = sorted(len(e.description or "") for e in entries)
    mid = len(lengths) // 2
    median_chars = float(lengths[mid]) if len(lengths) % 2 else (
        (lengths[mid - 1] + lengths[mid]) / 2.0)

    with_evidence = [e for e in entries if e.evidence]
    evidence_ratio = round(len(with_evidence) / len(entries), 4)

    grounded_files: set[str] = set()
    for e in with_evidence:
        for fp in (e.evidence.get("file_paths") or []):
            if fp in module_files:
                grounded_files.add(fp)
    concepts_grounded = sum(
        1 for e in with_evidence
        if any(fp in module_files for fp in (e.evidence.get("file_paths") or [])))

    return median_chars, evidence_ratio, concepts_grounded, len(grounded_files)


def _rules_with_provenance(project: str) -> int:
    try:
        from prism_service.services import ontology_rules

        return sum(1 for r in ontology_rules.rule_catalog(project)
                   if r.get("derived_from"))
    except Exception:
        return 0


def _open_rule_decisions(project: str) -> int:
    try:
        from prism_service.services.rule_decisions import _CLOSED_STATES
        from prism_service.services.signal_store import SignalStore

        store = SignalStore(project)
        return sum(
            1 for s in store.list(limit=500)
            if s.channel == "ontology" and s.state not in _CLOSED_STATES)
    except Exception:
        return 0


def _compute(project: str) -> dict:
    search_feedback_rate, recall_to_use_rate = _search_rates(project)
    module_files = _module_files(project)
    median_memory_chars, evidence_ratio, concepts_grounded_in_code, \
        modules_with_knowledge = _memory_metrics(project, module_files)

    return {
        "search_feedback_rate": search_feedback_rate,
        "recall_to_use_rate": recall_to_use_rate,
        "median_memory_chars": median_memory_chars,
        "evidence_ratio": evidence_ratio,
        "concepts_grounded_in_code": concepts_grounded_in_code,
        "modules_with_knowledge": modules_with_knowledge,
        "rules_with_provenance": _rules_with_provenance(project),
        "open_rule_decisions": _open_rule_decisions(project),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
