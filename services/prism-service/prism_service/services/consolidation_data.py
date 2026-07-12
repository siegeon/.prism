"""Pure data-access for the Consolidation page.

Extracted from app/ui/consolidation_page.py during the v5.0.0 cutover so
the React SPA's /api/consolidation endpoint can keep the same SQL without
depending on NiceGUI.

Parent task: 37932f3f · LL-11.
"""

from __future__ import annotations

import json
import sqlite3
from prism_service.services import sqlite_db
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_UNREFLECTED_THRESHOLD_HOURS = 24

# v6.3.41 (task c80fd9bb) — BOUNDED COST. A hard per-cycle candidate cap and a
# tokens/cycle ceiling enforced by a cheap deterministic gate that runs BEFORE
# any LLM call, so a backlog of candidates can never burn unbounded inference
# in one cycle (Reflexion bounded buffer; owner cost constraint 2026-06-08).
MAX_CANDIDATES_PER_CYCLE = 8
TOKENS_PER_CYCLE_CEILING = 60_000


def select_cycle_candidates(
    scores_db: str,
    max_candidates: int = MAX_CANDIDATES_PER_CYCLE,
    token_ceiling: int = TOKENS_PER_CYCLE_CEILING,
) -> list[dict]:
    """Cheap deterministic pre-LLM gate: pick at most ``max_candidates``
    pending candidates whose cumulative ``scope_json.est_tokens`` stays within
    ``token_ceiling``. Pure SQL + JSON, zero inference. The reflection consumer
    calls this to bound how much it may spend in one cycle; survivors (and only
    survivors) reach the LLM salience judge. Oldest-first (FIFO) so backlog
    drains fairly. Returns the selected candidate rows as dicts."""
    if not Path(scores_db).exists():
        return []
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM consolidation_candidates WHERE status='pending' "
            "ORDER BY queued_at ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    picked: list[dict] = []
    spent = 0
    for r in rows:
        if len(picked) >= max_candidates:
            break
        row = dict(r)
        try:
            est = int(json.loads(row.get("scope_json") or "{}").get("est_tokens", 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            est = 0
        if est < 0:
            est = 0
        if spent + est > token_ceiling:
            continue  # this one would breach the ceiling; try smaller ones
        spent += est
        picked.append(row)
    return picked


def get_queue_summary(scores_db: str) -> dict:
    """Return counts by status for consolidation_candidates."""
    keys = ("pending", "dispensed", "completed", "abandoned", "stale")
    if not Path(scores_db).exists():
        return {k: 0 for k in keys}
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM consolidation_candidates "
            "GROUP BY status"
        ).fetchall()
    finally:
        conn.close()
    counts = {k: 0 for k in keys}
    for r in rows:
        status = r["status"] or "unknown"
        if status in counts:
            counts[status] = int(r["n"])
    return counts


def get_unreflected_briefs(
    scores_db: str,
    age_hours: int = _UNREFLECTED_THRESHOLD_HOURS,
    now: datetime | None = None,
) -> list[dict]:
    """Pending candidates older than ``age_hours`` — work the reflection
    loop hasn't picked up yet."""
    if not Path(scores_db).exists():
        return []
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=age_hours)).isoformat()
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, task_id, trigger, queued_at, last_nudged_at, "
            "       retry_count, scope_json "
            "FROM consolidation_candidates "
            "WHERE status='pending' AND queued_at <= ? "
            "ORDER BY queued_at ASC",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    # v6.0.5 — surface signal_counts + transcript_excerpt for the SPA's
    # expand-to-see-content view. Old scope dicts (counts only) carry
    # an empty excerpt and zeroed signal_counts, which is the right
    # rendering for pre-v6.0.5 candidates.
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            scope = json.loads(d.pop("scope_json", None) or "{}")
        except Exception:
            scope = {}
        d["signal_counts"] = scope.get("signal_counts") or {}
        d["transcript_excerpt"] = scope.get("transcript_excerpt") or ""
        # v6.2.18 — flag legacy no-signal rows so the SPA can badge them
        # "noise". New imports are filtered at enqueue time, but rows that
        # predate the filter still sit pending until pruned; this lets the
        # pending list show WHY a brief is empty instead of looking broken.
        sc = d["signal_counts"]
        if (
            d.get("task_id") is None
            and all(int(sc.get(k) or 0) == 0 for k in (
                "pushbacks", "bg_signals", "tool_failures", "memory_writes",
            ))
        ):
            d["skip_reason"] = "noise — no task, zero signals"
        else:
            d["skip_reason"] = ""
        out.append(d)
    return out


def get_signal_rollup(scores_db: str) -> dict:
    """Aggregate signal_counts across all consolidation_candidates plus
    a reflections-run count. The /consolidation page renders this as a
    headline strip so the user can see what's been *extracted* from
    their sessions vs. what's been *reflected into memory* — which
    answers the question "what are we actually learning?"."""
    empty = {
        "sessions_scanned": 0, "pushbacks": 0, "bg_signals": 0,
        "tool_failures": 0, "memory_writes": 0,
        "reflections_run": 0, "memories_minted": 0,
    }
    if not Path(scores_db).exists():
        return empty
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT scope_json FROM consolidation_candidates"
        ).fetchall()
        run_row = conn.execute(
            "SELECT COUNT(*) AS n FROM consolidation_runs"
        ).fetchone()
    finally:
        conn.close()
    out = dict(empty)
    for r in rows:
        try:
            sc = json.loads(r["scope_json"] or "{}").get("signal_counts") or {}
            for k in ("pushbacks", "bg_signals", "tool_failures", "memory_writes"):
                out[k] += int(sc.get(k) or 0)
        except Exception:
            continue
    out["sessions_scanned"] = len(rows)
    out["reflections_run"] = int((run_row or {"n": 0})["n"])
    return out


def get_trends(scores_db: str, days: int = 14) -> dict:
    """v6.0.11 — Per-day rollup for the SPA sparkline strip.

    Buckets the last ``days`` UTC days by session_outcomes.timestamp
    and aggregates: sessions, total tool failures, total pushbacks
    (counts pulled from consolidation_candidates.scope_json). Days
    with no activity are present with zero values so the sparkline
    renders a continuous baseline.
    """
    from datetime import date, datetime, timedelta, timezone
    today = datetime.now(timezone.utc).date()
    buckets = {
        (today - timedelta(days=i)).isoformat(): {
            "sessions": 0, "pushbacks": 0, "tool_failures": 0,
            "bg_signals": 0, "memory_writes": 0, "reflections": 0,
        }
        for i in range(days)
    }
    if not Path(scores_db).exists():
        return {"days": sorted(buckets.keys()), "series": buckets}
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        # Sessions by day
        rows = conn.execute(
            "SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS n "
            "FROM session_outcomes WHERE substr(timestamp, 1, 10) >= ? "
            "GROUP BY day",
            ((today - timedelta(days=days - 1)).isoformat(),),
        ).fetchall()
        for r in rows:
            if r["day"] in buckets:
                buckets[r["day"]]["sessions"] = int(r["n"])
        # Signal counts per candidate, summed per day of queued_at
        rows = conn.execute(
            "SELECT substr(queued_at, 1, 10) AS day, scope_json "
            "FROM consolidation_candidates "
            "WHERE substr(queued_at, 1, 10) >= ?",
            ((today - timedelta(days=days - 1)).isoformat(),),
        ).fetchall()
        for r in rows:
            day = r["day"]
            if day not in buckets:
                continue
            try:
                sc = json.loads(r["scope_json"] or "{}").get("signal_counts") or {}
            except Exception:
                continue
            for k in ("pushbacks", "tool_failures", "bg_signals", "memory_writes"):
                buckets[day][k] += int(sc.get(k) or 0)
        # Reflections per day
        rows = conn.execute(
            "SELECT substr(run_at, 1, 10) AS day, COUNT(*) AS n "
            "FROM consolidation_runs WHERE substr(run_at, 1, 10) >= ? "
            "GROUP BY day",
            ((today - timedelta(days=days - 1)).isoformat(),),
        ).fetchall()
        for r in rows:
            if r["day"] in buckets:
                buckets[r["day"]]["reflections"] = int(r["n"])
    finally:
        conn.close()
    return {"days": sorted(buckets.keys()), "series": buckets}


def get_recent_runs(scores_db: str, limit: int = 20) -> list[dict]:
    """Recent consolidation_runs with a short narrative excerpt."""
    if not Path(scores_db).exists():
        return []
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, candidate_id, run_at, output_json, subagent_type, confidence "
            "FROM consolidation_runs ORDER BY run_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            payload = json.loads(d.get("output_json") or "{}")
            d["narrative_excerpt"] = (payload.get("narrative") or "")[:240]
        except Exception:
            d["narrative_excerpt"] = ""
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# session_outcomes → consolidation_candidates bridge
# ---------------------------------------------------------------------------
#
# The Stop hook path (stop_record_hook.py → janitor_enqueue MCP) is the
# canonical producer of candidates, but it only fires when (a) Claude
# Code is configured against the project's MCP endpoint and (b) there's
# an `in_progress` task tagged with a merge_sha. Two cases miss:
#
#   * Isolated/preview MCP instances (v51 on :8887) that the user never
#     drives a full Claude session against — /consolidation stays empty
#     even though session_outcomes has data.
#   * Sessions that ran without an in_progress task linked.
#
# Bridge: one candidate per session_outcome.session_id. We own both
# sides — the recorder (record_session_outcome) calls enqueue_for_session
# on insert, and an on-demand backfill endpoint sweeps everything
# historical. Idempotent on session_id, so calling twice is a no-op.

def resolve_task_id_for_session(scores_db: str, session_id: str) -> str | None:
    """Return the task_id linked to ``session_id`` via task_sessions, or
    None. The transcript disk path has no task_id of its own; when a
    session was task-linked (conductor_advance/task_link_session writes
    task_sessions), the consolidation_candidate must carry that task_id
    so /learning's Layer-B rollup can fill (FIX 2a)."""
    if not session_id or not Path(scores_db).exists():
        return None
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    try:
        try:
            row = conn.execute(
                "SELECT task_id FROM task_sessions WHERE session_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # table absent on a fresh db
    finally:
        conn.close()
    return row[0] if row else None


def enqueue_for_session(
    scores_db: str,
    session_id: str,
    scope: dict | None = None,
    trigger: str = "session_completed",
    task_id: str | None = None,
) -> str | None:
    """Insert a pending consolidation_candidate for ``session_id`` if one
    doesn't already exist. Returns the new candidate id, or None when
    the session already had one (idempotent). When ``task_id`` is None it
    is resolved from task_sessions so a task-linked session stamps its
    task_id onto the candidate (FIX 2a)."""
    if not session_id or not Path(scores_db).exists():
        return None
    if task_id is None:
        task_id = resolve_task_id_for_session(scores_db, session_id)
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT id FROM consolidation_candidates WHERE session_id=? LIMIT 1",
            (session_id,),
        ).fetchone()
        if existing:
            return None
        cid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, scope_json, status, queued_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (
                cid, task_id, session_id, trigger,
                json.dumps(scope or {}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cid
    finally:
        conn.close()


def backfill_from_sessions(scores_db: str, limit: int = 500) -> dict:
    """Sweep session_outcomes and enqueue a candidate for any session
    that doesn't already have one. Returns {created, skipped, scanned}."""
    if not Path(scores_db).exists():
        return {"created": 0, "skipped": 0, "scanned": 0}
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT session_id FROM session_outcomes "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    created = 0
    skipped = 0
    for r in rows:
        sid = r["session_id"]
        if enqueue_for_session(scores_db, sid):
            created += 1
        else:
            skipped += 1
    return {"created": created, "skipped": skipped, "scanned": len(rows)}


def prune_noise_candidates(scores_db: str) -> int:
    """One-shot drain of the no-signal candidate pile.

    DELETE every *pending* consolidation_candidate that carries no usable
    signal — task_id IS NULL AND every signal_counts bucket is zero. This
    is the cleanup companion to the v6.2.18 enqueue-time noise filter:
    the filter stops new noise, this drains the backlog that accumulated
    before it. Idempotent — a second call finds nothing and returns 0.
    Returns the number of rows deleted.
    """
    if not Path(scores_db).exists():
        return 0
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, task_id, scope_json FROM consolidation_candidates "
            "WHERE status='pending'"
        ).fetchall()
        doomed: list[str] = []
        for r in rows:
            if r["task_id"] is not None:
                continue
            try:
                sc = json.loads(r["scope_json"] or "{}").get("signal_counts") or {}
            except Exception:
                sc = {}
            if all(int(sc.get(k) or 0) == 0 for k in (
                "pushbacks", "bg_signals", "tool_failures", "memory_writes",
            )):
                doomed.append(r["id"])
        for cid in doomed:
            conn.execute(
                "DELETE FROM consolidation_candidates WHERE id=?", (cid,)
            )
        conn.commit()
        return len(doomed)
    finally:
        conn.close()


# Default retention windows — how long a completed candidate / session
# outcome is kept before the governance sweep prunes it.
_CANDIDATE_RETENTION_DAYS = 30
_SESSION_OUTCOME_CAP = 5000


def retention_sweep(
    scores_db: str,
    candidate_retention_days: int = _CANDIDATE_RETENTION_DAYS,
    session_outcome_cap: int = _SESSION_OUTCOME_CAP,
    now: datetime | None = None,
) -> dict:
    """FIX 4a — bounded, idempotent retention sweep on the governance
    cadence.

    Deletes ONLY rows that have aged out, never the working set:
      * consolidation_candidates with status='completed' whose
        completed_at is older than ``candidate_retention_days`` — pending
        candidates (any age) and recent completed rows always survive;
      * the oldest session_outcomes beyond ``session_outcome_cap`` rows,
        so the session log can't grow unbounded.

    Returns {candidates_pruned, session_outcomes_pruned} so the timer can
    log what it pruned. Idempotent — a second call finds nothing aged out
    and prunes 0.
    """
    report = {"candidates_pruned": 0, "session_outcomes_pruned": 0}
    if not Path(scores_db).exists():
        return report
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=candidate_retention_days)).isoformat()
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    try:
        cur = conn.execute(
            "DELETE FROM consolidation_candidates "
            "WHERE status='completed' AND completed_at IS NOT NULL "
            "AND completed_at < ?",
            (cutoff,),
        )
        report["candidates_pruned"] = cur.rowcount or 0
        # Cap session_outcomes to the newest N rows (archive the tail by
        # deleting it — the reflected signal already lives in candidates).
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM session_outcomes"
            ).fetchone()[0]
            if total > session_outcome_cap:
                cur2 = conn.execute(
                    "DELETE FROM session_outcomes WHERE session_id IN ("
                    "  SELECT session_id FROM session_outcomes "
                    "  ORDER BY timestamp DESC LIMIT -1 OFFSET ?"
                    ")",
                    (session_outcome_cap,),
                )
                report["session_outcomes_pruned"] = cur2.rowcount or 0
        except sqlite3.OperationalError:
            pass  # session_outcomes absent on a bare db
        conn.commit()
    finally:
        conn.close()
    return report
