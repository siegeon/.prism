"""Consolidation API — reflection queue state, unreflected briefs, recent runs."""

import json as _json
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Query

from prism_service.project_context import get_project
from prism_service.services.consolidation_data import (
    get_queue_summary, get_unreflected_briefs, get_recent_runs,
    backfill_from_sessions, get_signal_rollup, get_trends,
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
        "trends": get_trends(scores_db, days=14),
    }


@router.get("/workers")
def workers() -> dict:
    """v6.0.9 — Honest snapshot of what's running vs what isn't. v6.0.18
    splits the reflection_worker entry by PRISM_REFLECTION_WORKER env
    state so the panel reports reality rather than a fixed 'NOT
    RUNNING by design' string."""
    reflection_on = os.environ.get("PRISM_REFLECTION_WORKER", "").lower() in (
        "1", "on", "true", "yes",
    )
    reflection_interval = int(
        os.environ.get("PRISM_REFLECTION_WORKER_INTERVAL", "900")
    )
    if reflection_on:
        reflection_entry = {
            "id": "reflection_worker",
            "label": "Reflection worker",
            "running": True,
            "cadence_s": reflection_interval,
            "description": (
                "Opt-in (PRISM_REFLECTION_WORKER=on). Picks the oldest "
                "pending consolidation_candidate every "
                f"{reflection_interval}s, dispatches the same headless "
                "claude_cli path /api/consolidation/run-reflection uses, "
                "and persists the verdict + minted memories."
            ),
        }
    else:
        reflection_entry = {
            "id": "reflection_worker",
            "label": "Reflection worker",
            "running": False,
            "cadence_s": 0,
            "description": (
                "OFF — set PRISM_REFLECTION_WORKER=on (and optionally "
                "PRISM_REFLECTION_WORKER_INTERVAL, default 900s) to "
                "drain pending briefs automatically. Until then, "
                "/learning + /memory only fill when you click Reflect "
                "on /consolidation or run /prism-reflect from a session."
            ),
        }
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
            reflection_entry,
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


@router.post("/run-reflection")
def run_reflection(
    candidate_id: str = Query(...),
    project: str = Query("default"),
) -> dict:
    """Manual reflection trigger.

    v6.0.18 — body moved to services.reflection_runner.run_one so the
    PRISM_REFLECTION_WORKER daemon can dispatch the same path. The
    runner shells out via inference.claude_cli (max_turns=15,
    Read/Glob/Grep + a curated mcp__prism__* allowlist), parses the
    JSON verdict, calls JanitorService.submit, and stores
    verdict.new_memories. Failure modes return a structured error
    rather than 500: candidate not found / not pending, claude CLI not
    logged in, verdict not parseable as JSON.
    """
    from prism_service.services.reflection_runner import run_one
    return run_one(project=project, candidate_id=candidate_id).to_dict()


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
