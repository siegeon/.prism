"""Consolidation API — reflection queue state, unreflected briefs, recent runs."""

import json as _json
import os
import sqlite3
from prism_service.services import sqlite_db
from pathlib import Path

from fastapi import APIRouter, Query

from prism_service.project_context import get_project
from prism_service.services.consolidation_data import (
    get_queue_summary, get_unreflected_briefs, get_recent_runs,
    backfill_from_sessions, get_signal_rollup, get_trends,
    prune_noise_candidates,
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
def workers(project: str = Query("default")) -> dict:
    """Honest snapshot of what's running vs what isn't.

    Phase 5 (epic 4fd1e6b4) COLLAPSES the memory concern. The discrete
    reflection_worker + memory_summary_worker rows are gone; the memory
    learning pipeline is now presented as:
      - kind=="pipeline" rows — the event-driven learning pipeline (events
        flow through the EventBus → ConsumerPool), reporting live throughput
        (events_per_min), queue depth, last-event timestamp, and in-flight
        count, plus a per-pass consolidation receipt for progressive
        disclosure.
      - kind=="clock"    row  — the single Phase-4 maintenance clock,
        reporting interval / last sweep / next sweep.
    Non-memory infra/brain workers (transcript importer, drift reindex,
    understand drainer, trash sweeper, auto-updater) stay listed separately
    as kind=="worker" rows. Each row carries a `kind` so the SPA panel can
    branch its render."""
    return {
        "workers": [
            {
                "id": "transcript_importer",
                "kind": "worker",
                "label": "Transcript importer",
                "running": True,
                "cadence_s": 60,
                "description": (
                    "Polls ~/.claude/projects/<slug>/*.jsonl every 60s, "
                    "imports unseen session_outcomes + skill_usage, and "
                    "(v6.0.5+) enqueues a consolidation_candidate with "
                    "transcript_excerpt for each."
                ),
                "prompt_kind": "none",
            },
            {
                "id": "drift_timer",
                "kind": "worker",
                "label": "Brain drift reindex",
                "running": True,
                "cadence_s": 1800,
                "description": "Reindexes drifted Brain docs per project on a cadence.",
                "prompt_kind": "none",
            },
            {
                "id": "understand_drainer",
                "kind": "worker",
                "label": "Understand analyzer drainer",
                "running": True,
                "cadence_s": 0,
                "description": "Pulls analyzer jobs off the queue, runs them, writes results.",
                "prompt_kind": "per_job",
                "prompt": (
                    "Each analyzer carries its own prompt template; the "
                    "drainer renders one per claimed job. Click a job "
                    "row below to see the exact prompt sent for that "
                    "run (v6.1.1+)."
                ),
            },
            {
                "id": "trash_sweeper",
                "kind": "worker",
                "label": "Trash sweeper",
                "running": True,
                "cadence_s": 30,
                "description": "rmtree's soft-deleted project dirs once SQLite locks release.",
                "prompt_kind": "none",
            },
            {
                "id": "auto_updater",
                "kind": "worker",
                "label": "Auto-updater",
                "running": True,
                "cadence_s": 1800,
                "description": "Polls GitHub Releases, applies newer wheel via pip.",
                "prompt_kind": "none",
            },
            _event_pipeline_row(project),
            _maintenance_clock_row(),
        ],
    }


def _event_pipeline_row(project: str) -> dict:
    """The collapsed event-driven learning pipeline row (kind=='pipeline').

    Sources live metrics off the process-singleton EventBus (event_pool):
    throughput, queue depth, last-event timestamp, and in-flight count — the
    four readouts that replace the discrete reflection_worker /
    memory_summary_worker rows. Also carries the latest per-pass
    consolidation receipt (extracted / deduped / superseded / retired) for
    the panel's progressive-disclosure summary."""
    from prism_service.services import event_pool as ep
    bus = ep.get_bus()
    return {
        "id": "memory_learning_pipeline",
        "kind": "pipeline",
        "label": "Memory learning pipeline",
        "running": ep.is_enabled(),
        "cadence_s": ep._interval_s(),
        "events_per_min": bus.events_per_min(),
        "queue_depth": bus.queue_depth(),
        "last_event_ts": bus.last_event_ts(),
        "in_flight": bus.in_flight(),
        "receipt": _latest_consolidation_receipt(project),
        "description": (
            "Event-driven learning: session.imported / memory.written / "
            "memory.recalled+outcome flow through the in-process EventBus to "
            "the budget-governed consumer pool — the single claude -p "
            "chokepoint that replaced the old reflection + memory-summary "
            "timers. Folds reflection + summary minting onto one pipeline."
        ),
    }


def _latest_consolidation_receipt(project: str) -> dict | None:
    """Last consolidation pass receipt for progressive disclosure: how many
    memories the most recent reflection extracted / deduped / superseded /
    retired. Reads the recent runs already surfaced by the consolidation
    overview; returns None when there are no runs yet."""
    try:
        ctx = get_project(project)
        scores_db = str(ctx._data_dir / "scores.db")
        if not Path(scores_db).exists():
            return None
        runs = get_recent_runs(scores_db, limit=1)
        if not runs:
            return None
        r = runs[0] or {}
        payload = _json.loads(r.get("output_json") or "{}")
        new_mem = payload.get("new_memories") or []
        invalidated = payload.get("invalidate_memory_ids") or []
        return {
            "extracted": len(new_mem) if isinstance(new_mem, list)
            else int(payload.get("extracted", 0) or 0),
            "deduped": int(payload.get("deduped", 0) or 0),
            "superseded": len(invalidated) if isinstance(invalidated, list)
            else int(payload.get("superseded", 0) or 0),
            "retired": int(payload.get("retired", 0) or 0),
            "at": r.get("run_at"),
        }
    except Exception:
        return None


def _maintenance_clock_row() -> dict:
    """The single Phase-4 maintenance clock row (kind=='clock').

    The five wall-clock memory duties (governance TTL+decay+dup,
    verify_staleness, forget, adaptive retune, quality-vs-git) are folded
    into ONE heartbeat. Reports interval / last sweep / next sweep alongside
    each pass's own cadence gate."""
    from prism_service.services import maintenance_clock as mc
    _cadences = mc.pass_cadences()
    _enabled = mc.pass_enabled()
    _on = [n for n in mc.PASS_ORDER if _enabled.get(n)]
    _off = [n for n in mc.PASS_ORDER if not _enabled.get(n)]

    def _fmt_cadence(s: int) -> str:
        if s % 3600 == 0:
            return f"{s // 3600}h"
        if s % 60 == 0:
            return f"{s // 60}m"
        return f"{s}s"

    _per_pass = ", ".join(f"{n}~{_fmt_cadence(_cadences[n])}" for n in mc.PASS_ORDER)
    return {
        "id": mc.WORKER_ID,
        "kind": "clock",
        "label": mc.WORKER_LABEL,
        "running": mc.is_enabled(),
        "cadence_s": mc.heartbeat_interval_s(),
        "interval_s": mc.heartbeat_interval_s(),
        "last_sweep": mc.last_sweep_ts(),
        "next_sweep": mc.next_sweep_ts(),
        "passes": {n: {"cadence_s": _cadences[n], "enabled": _enabled.get(n, False)}
                   for n in mc.PASS_ORDER},
        "description": (
            "ONE heartbeat thread (folds the prior 4-5 separate memory "
            "timers). Each tick iterates every project and runs, in sequence, "
            "five memory passes — each behind its OWN cadence gate: "
            f"{_per_pass}. Passes ON: {', '.join(_on) or 'none'}. "
            f"Passes OFF (env-gated): {', '.join(_off) or 'none'}. Honors "
            "PRISM_GOVERNANCE_INTERVAL / PRISM_QUALITY_INTERVAL / "
            "PRISM_ADAPTIVE_POLICY_WORKER[_INTERVAL] / PRISM_<OP>_WORKER; "
            "disable the whole fold with PRISM_MAINTENANCE_CLOCK=off."
        ),
        "prompt_kind": "none",
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
    conn = sqlite_db.connect(scores_db, timeout=5.0)
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


@router.post("/drain")
def drain(project: str = Query("default")) -> dict:
    """FIX 1d — one-call backlog drain. Runs the reflection worker once,
    pinned to this project, to process the existing pending real-signal
    candidates in a single request (the noise filter still skips no-signal
    briefs). Reports how many it ran. The PRISM_REFLECTION_WORKER daemon
    does the same on a cadence; this is the manual catch-up button."""
    import os as _os
    from prism_service.services import reflection_worker as rw
    prev = _os.environ.get("PRISM_REFLECTION_WORKER_PROJECT")
    _os.environ["PRISM_REFLECTION_WORKER_PROJECT"] = project
    try:
        summary = rw.run_once()
    finally:
        if prev is None:
            _os.environ.pop("PRISM_REFLECTION_WORKER_PROJECT", None)
        else:
            _os.environ["PRISM_REFLECTION_WORKER_PROJECT"] = prev
    return {"drained": summary.get("ran", 0), **summary}


@router.post("/prune-noise")
def prune_noise(project: str = Query("default")) -> dict:
    """v6.2.18 — One-click drain of the no-signal candidate pile.

    Deletes pending consolidation_candidates that carry no usable signal
    (task_id NULL AND all signal_counts zero) — the backlog the v6.2.18
    enqueue-time noise filter now prevents going forward. Idempotent:
    a second call returns {deleted: 0}."""
    ctx = get_project(project)
    scores_db = str(ctx._data_dir / "scores.db")
    return {"deleted": prune_noise_candidates(scores_db)}
