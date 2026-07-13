"""Pure data-access for the Learning page.

Extracted from app/ui/learning_page.py during the v5.0.0 cutover so the
React SPA's /api/learning endpoint can keep the same SQL without
depending on NiceGUI.

Parent task: 37932f3f · LL-11.
"""

from __future__ import annotations

import json
import sqlite3
from prism_service.services import sqlite_db
from pathlib import Path

# Threshold below which variant rankings are flagged as correlational.
# Matches the number used throughout the learning-loop docs.
_CORRELATIONAL_THRESHOLD = 20


def get_learning_rows(scores_db: str, limit: int = 50) -> list[dict]:
    """Return recent task_quality_rollup rows ordered newest-first."""
    if not Path(scores_db).exists():
        return []
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT task_id, quality_score, cuped_score, "
            "       qualitative_score, components_json, scored_at "
            "FROM task_quality_rollup "
            "ORDER BY scored_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_variant_performance(
    scores_db: str, n_threshold: int = _CORRELATIONAL_THRESHOLD,
) -> list[dict]:
    """Aggregate CUPED-adjusted quality per prompt_id across scored tasks.
    Each row carries a ``correlational`` flag set when sample count hasn't
    crossed the ``n_threshold`` reliability gate."""
    if not Path(scores_db).exists():
        return []
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT tv.prompt_id, tv.persona, "
            "       AVG(COALESCE(qr.cuped_score, qr.quality_score)) AS avg_score, "
            "       COUNT(*) AS n "
            "FROM task_variants tv "
            "JOIN task_quality_rollup qr ON qr.task_id = tv.task_id "
            "GROUP BY tv.prompt_id, tv.persona "
            "ORDER BY avg_score DESC"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["correlational"] = int(d["n"]) < n_threshold
        out.append(d)
    return out


def get_recent_reflections(scores_db: str, limit: int = 25) -> list[dict]:
    """Return recent consolidation_runs flattened for the /learning page.

    /learning's primary table (task_quality_rollup) only fills when a
    candidate has a task_id, but transcript-derived candidates only
    carry session_id — so the page stayed empty even after a reflection
    landed. This pulls the runs directly so users see something happen
    after they click Reflect on /consolidation, regardless of whether
    the candidate had a task_id.
    """
    if not Path(scores_db).exists():
        return []
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT r.id, r.candidate_id, r.run_at, r.subagent_type, "
            "       r.confidence, r.output_json, "
            "       c.task_id AS candidate_task_id, "
            "       c.session_id AS candidate_session_id, "
            "       c.trigger AS candidate_trigger "
            "FROM consolidation_runs r "
            "LEFT JOIN consolidation_candidates c ON c.id = r.candidate_id "
            "ORDER BY r.run_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            payload = json.loads(d.pop("output_json", None) or "{}")
        except Exception:
            payload = {}
        d["qualitative_score"] = payload.get("qualitative_score")
        d["narrative_excerpt"] = (payload.get("narrative") or "")[:280]
        d["memories_minted"] = len(payload.get("new_memories") or [])
        out.append(d)
    return out
