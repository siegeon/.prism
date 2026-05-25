"""Consolidation API — reflection queue state, unreflected briefs, recent runs."""

import json as _json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Query

from prism_service.project_context import get_project
from prism_service.services.consolidation_data import (
    get_queue_summary, get_unreflected_briefs, get_recent_runs,
    backfill_from_sessions, get_signal_rollup,
)
from prism_service.services.janitor_service import _DEFAULT_MCPS

router = APIRouter()


@router.get("")
def overview(
    project: str = Query("default"),
    runs_limit: int = Query(20, ge=1, le=200),
    # 0 means "every pending brief, regardless of age". The 24-h cap
    # is the *forgotten work* filter — usable but it hides freshly
    # backfilled candidates, which made the page look empty after the
    # bridge added 21 brand-new rows. Default to showing them all and
    # let the UI bucket by age.
    pending_age_hours: int = Query(0, ge=0, le=720),
) -> dict:
    ctx = get_project(project)
    scores_db = str(ctx._data_dir / "scores.db")
    return {
        "queue": get_queue_summary(scores_db),
        "unreflected": get_unreflected_briefs(scores_db, age_hours=pending_age_hours),
        "recent_runs": get_recent_runs(scores_db, limit=runs_limit),
        "signal_rollup": get_signal_rollup(scores_db),
    }


@router.get("/workers")
def workers() -> dict:
    """v6.0.9 — Honest snapshot of what's running vs what isn't. The
    answer to 'is anything automating reflection?' is currently no, by
    design: PRISM runs zero LLMs, reflection requires a Claude session
    to dispatch via /prism-reflect. This endpoint makes that explicit
    on the SPA instead of leaving the user to guess."""
    return {
        "workers": [
            {
                "id": "transcript_importer",
                "label": "Transcript importer",
                "running": True,
                "cadence_s": 60,
                "description": (
                    "Polls ~/.claude/projects/<slug>/*.jsonl every 60s, "
                    "imports unseen session_outcomes + skill_usage, and "
                    "(v6.0.5+) enqueues a consolidation_candidate with "
                    "transcript_excerpt for each."
                ),
            },
            {
                "id": "drift_timer",
                "label": "Brain drift reindex",
                "running": True,
                "cadence_s": 1800,
                "description": "Reindexes drifted Brain docs per project on a cadence.",
            },
            {
                "id": "understand_drainer",
                "label": "Understand analyzer drainer",
                "running": True,
                "cadence_s": 0,
                "description": "Pulls analyzer jobs off the queue, runs them, writes results.",
            },
            {
                "id": "governance_timer",
                "label": "Governance",
                "running": True,
                "cadence_s": 3600,
                "description": "Per-domain health checks + janitor sweeps.",
            },
            {
                "id": "trash_sweeper",
                "label": "Trash sweeper",
                "running": True,
                "cadence_s": 30,
                "description": "rmtree's soft-deleted project dirs once SQLite locks release.",
            },
            {
                "id": "auto_updater",
                "label": "Auto-updater",
                "running": True,
                "cadence_s": 1800,
                "description": "Polls GitHub Releases, applies newer wheel via pip.",
            },
            {
                "id": "reflection_worker",
                "label": "Reflection worker",
                "running": False,
                "cadence_s": 0,
                "description": (
                    "NOT RUNNING by design — PRISM is a zero-LLM service. "
                    "Reflection requires a Claude session to dispatch the "
                    "prism-reflect sub-agent. Until that's wired (via the "
                    "/prism-reflect slash command, a SessionStart hint, or "
                    "a future opt-in headless worker), pending briefs "
                    "stay queued and /learning + /memory stay un-fed."
                ),
            },
        ],
    }


@router.get("/next-brief")
def next_brief(project: str = Query("default")) -> dict:
    """v6.0.9 — Preview the brief the reflection sub-agent WOULD see if
    invoked right now. Reads JanitorService.check without dispensing
    (no state change) so the SPA can render what the agent's input
    looks like — closes the loop on 'what would reflection even do'."""
    ctx = get_project(project)
    scores_db = str(ctx._data_dir / "scores.db")
    if not Path(scores_db).exists():
        return {"ready": False, "brief": None, "reason": "no scores.db yet"}
    # Non-mutating preview: read the oldest pending candidate directly
    # rather than calling JanitorService.check() (which would flip its
    # status to dispensed).
    conn = sqlite3.connect(scores_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, task_id, session_id, scope_json, trigger, queued_at "
            "FROM consolidation_candidates "
            "WHERE status='pending' ORDER BY queued_at ASC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ready": False, "brief": None, "reason": "no pending candidates"}
    try:
        scope = _json.loads(row["scope_json"] or "{}")
    except Exception:
        scope = {}
    return {
        "ready": True,
        "candidate_id": row["id"],
        "task_id": row["task_id"],
        "session_id": row["session_id"],
        "trigger": row["trigger"],
        "queued_at": row["queued_at"],
        "mcps_available": list(_DEFAULT_MCPS),
        "response_schema_keys": [
            "qualitative_score", "narrative", "new_memories",
            "invalidate_memory_ids", "confidence",
        ],
        "scope": scope,
    }


@router.post("/backfill")
def backfill(project: str = Query("default"), limit: int = Query(500, ge=1, le=5000)) -> dict:
    """Enqueue a consolidation_candidate for every session_outcome that
    doesn't already have one. Idempotent on session_id — re-running
    just returns {created: 0, skipped: N}. Useful for populating the
    page on instances where the Stop hook never fired, or for catching
    up after the bridge was added."""
    ctx = get_project(project)
    scores_db = str(ctx._data_dir / "scores.db")
    return backfill_from_sessions(scores_db, limit=limit)
