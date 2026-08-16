"""Pure data-access for the agent-run telemetry spine (task f4498190).

Mirrors learning_data.py: every function takes ``scores_db: str``, guards
``Path(scores_db).exists()``, opens ``sqlite3.connect`` with a Row factory,
and returns plain dicts/lists. No FastAPI / project_context coupling.

The spine is the self-heal / self-learn input: per-agent/subagent run rows
keyed (run_id, agent_id, step). Writes UPSERT on that triple so a re-POST of
the same step updates rather than duplicates.
"""

from __future__ import annotations

import sqlite3
from prism_service.services import sqlite_db
from pathlib import Path

# Columns persisted on agent_runs (order == the ingest payload contract).
_COLS = (
    "run_id", "workflow_name", "task_id", "session_id", "agent_id",
    "parent_agent_id", "role", "step", "model", "started_at", "ended_at",
    "duration_ms", "tokens", "tool_uses", "ok", "gate_state",
    "verdict_summary", "evidence_ref",
)

# Filterable GET params -> agent_runs columns.
_FILTERS = ("task_id", "session_id", "workflow_name", "role", "step")

# gate_state values that READ AS A PASS to whoever judges the row. Live data
# also carries compound "<gate>:<state>" forms ("story_gate:pending"), so the
# trailing verdict segment is checked too.
_PASSING_GATE_STATES = frozenset({
    "passed", "pass", "passing", "approved", "approve",
    "green", "greenlit", "ok", "success", "succeeded",
})

# Machine-readable name for the refusal below, so a driver that gets turned
# away can self-diagnose instead of guessing.
PRODUCER_GATE_VERDICT_REFUSAL = "producer_cannot_record_gate_verdict"

# Canonical agent_runs schema, single-sourced HERE (the module that owns
# every read/write of the table) so brain_engine.py's full scores.db init
# imports this constant instead of keeping its own copy -- one definition,
# never two that can drift apart. See _connect() below for why this module
# also applies it directly rather than only relying on BrainEngine's init.
AGENT_RUNS_SCHEMA = """
    -- Per-agent/subagent run telemetry spine (task f4498190). One row
    -- per (run_id, agent_id, step); idempotent upsert so a re-POST of
    -- the same triple UPDATES rather than duplicates. Feeds self-heal
    -- (flagging error/stall/token-heavy steps) + the /learning agent
    -- timeline panel + the per-task agent cost/path rollup.
    CREATE TABLE IF NOT EXISTS agent_runs (
        run_id TEXT NOT NULL,
        workflow_name TEXT,
        task_id TEXT,
        session_id TEXT,
        agent_id TEXT NOT NULL,
        parent_agent_id TEXT,
        role TEXT,
        step TEXT NOT NULL,
        model TEXT,
        started_at TEXT,
        ended_at TEXT,
        duration_ms INTEGER,
        tokens INTEGER,
        tool_uses INTEGER,
        ok INTEGER,
        gate_state TEXT,
        verdict_summary TEXT,
        evidence_ref TEXT,
        recorded_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (run_id, agent_id, step)
    );
    CREATE INDEX IF NOT EXISTS idx_agent_runs_task_id
        ON agent_runs(task_id);
    CREATE INDEX IF NOT EXISTS idx_agent_runs_session_id
        ON agent_runs(session_id);
    CREATE INDEX IF NOT EXISTS idx_agent_runs_workflow
        ON agent_runs(workflow_name);
"""


def is_passing_gate_state(value) -> bool:
    """True when ``value`` reads as a PASSING gate verdict.

    Deliberately NARROW: absent/None/""/"none"/"pending"/"failed" and
    compound forms like "story_gate:pending" are ordinary telemetry and
    must keep flowing untouched (the shared fixture at
    tests/unit/test_api_agent_runs.py:62 posts "none" on all 10 of its
    tests). Only pass-shaped values are treated as a claimed verdict.
    """
    if value is None:
        return False
    text = str(value).strip().lower()
    if not text:
        return False
    verdict = text.rsplit(":", 1)[-1].strip()
    return text in _PASSING_GATE_STATES or verdict in _PASSING_GATE_STATES


def producer_gate_verdict_reason(row: dict) -> str:
    """"" when the row is acceptable telemetry, else a NAMED sentence
    saying why it is refused.

    Same shape as the conductor's distinct-actor refusal: a reason string
    rather than a silent drop, because a tooth that computes a refusal and
    throws it away has only half-shipped.

    It reads ONLY the verdict being claimed, never who the caller says it
    is. Identity in a payload is a string the caller types, so keying on
    it would block only the honest. The real foothold is structural: the
    conductor's own seat builds its row in-process and calls
    upsert_agent_run directly, so it never arrives through this door.
    """
    if not is_passing_gate_state((row or {}).get("gate_state")):
        return ""
    return (
        "a producing actor cannot record a gate verdict: gate state is "
        "owned by the conductor, which writes its own seat's decision "
        "in-process. Re-post this telemetry row without a passing "
        "gate_state -- telemetry records what you did, never a verdict."
    )


def gate_source_for_row(row) -> str | None:
    """How a PASSING verdict got into the spine, so a reader can tell a
    genuine machine decision from a producer-written one without squinting
    at null timings by hand.

    "machine" carries the server-clock signature only the conductor's
    in-process writer stamps (a real start instant AND a measured
    duration); "unattributed" is a pass with no such signature -- the
    shape of every producer-written row predating the ingest refusal,
    which stays readable and is annotated rather than deleted. None for
    ordinary telemetry, which claims no verdict at all.
    """
    if not is_passing_gate_state(row.get("gate_state")):
        return None
    started = str(row.get("started_at") or "").strip()
    measured = row.get("duration_ms")
    return "machine" if started and measured is not None else "unattributed"


def _connect(scores_db: str) -> sqlite3.Connection:
    """Open scores_db through the shared hardening funnel AND guarantee
    agent_runs exists (fresh-install ordering hole: BrainEngine's
    _init_scores_schema is the table's only creator today, but it only
    runs lazily on FIRST ACCESS of ProjectContext.brain_svc -- so a fresh
    data dir whose first-ever touch is POST /api/agent-runs/ingest 500s
    with "no such table: agent_runs" until something unrelated warms
    brain_svc). CREATE TABLE/INDEX IF NOT EXISTS is a cheap no-op once
    BrainEngine has already run its own init, so this never conflicts
    with it -- it just removes the ordering dependency on the ingest
    path, without paying BrainEngine's heavy construction cost
    (embeddings/FTS/vector setup) here."""
    conn = sqlite_db.connect(scores_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(AGENT_RUNS_SCHEMA)
    return conn


def upsert_agent_run(scores_db: str, row: dict) -> None:
    """Insert or update one telemetry row, idempotent on
    (run_id, agent_id, step). Booleans are coerced to 0/1 for sqlite."""
    vals = []
    for c in _COLS:
        v = row.get(c)
        if isinstance(v, bool):
            v = int(v)
        vals.append(v)
    placeholders = ", ".join("?" for _ in _COLS)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in _COLS
        if c not in ("run_id", "agent_id", "step")
    )
    sql = (
        f"INSERT INTO agent_runs ({', '.join(_COLS)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(run_id, agent_id, step) DO UPDATE SET {updates}"
    )
    conn = _connect(scores_db)
    try:
        conn.execute(sql, vals)
        conn.commit()
    finally:
        conn.close()


def get_agent_runs(scores_db: str, limit: int = 500, **filters) -> list[dict]:
    """Return agent_runs rows, newest-first, honoring task_id/session_id/
    workflow_name/role/step filters (None/empty filters are ignored)."""
    if not Path(scores_db).exists():
        return []
    where, params = [], []
    for k in _FILTERS:
        v = filters.get(k)
        if v:
            where.append(f"{k} = ?")
            params.append(v)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    conn = _connect(scores_db)
    try:
        rows = conn.execute(
            "SELECT * FROM agent_runs"
            f"{clause} ORDER BY started_at DESC, recorded_at DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["ok"] = bool(d.get("ok")) if d.get("ok") is not None else None
        # Derived at READ time, never stored: history stays byte-for-byte
        # as it was written, and every reader gets the provenance.
        d["gate_source"] = gate_source_for_row(d)
        out.append(d)
    return out


def get_task_agent_rollup(scores_db: str, task_id: str) -> dict:
    """Roll a task's agent_runs into total token cost + the ordered
    agent-path (role/step in chronological order). The Tier-3 self-learn
    signal: how much each task cost across its agents and the path taken."""
    if not Path(scores_db).exists():
        return {}
    conn = _connect(scores_db)
    try:
        rows = conn.execute(
            "SELECT role, step, model, tokens, duration_ms, started_at "
            "FROM agent_runs WHERE task_id = ? "
            "ORDER BY started_at ASC, recorded_at ASC",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {}
    path = [dict(r) for r in rows]
    total_tokens = sum(int(r["tokens"] or 0) for r in rows)
    total_duration = sum(int(r["duration_ms"] or 0) for r in rows)
    return {
        "task_id": task_id,
        "total_tokens": total_tokens,
        "total_duration_ms": total_duration,
        "agent_count": len(path),
        "agent_path": path,
    }


_UUID_RE = None  # compiled lazily; module keeps stdlib-only imports at top


def _ts_epoch(ts) -> float | None:
    """Parse an agent_runs timestamp into epoch seconds. Rows carry either
    epoch floats stored as text ("1783443408.499"), legacy naive
    "YYYY-MM-DD HH:MM:SS", or ISO strings. None when unparseable."""
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace(" ", "T").replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _is_real_session(sid: str) -> bool:
    """A real (UUID) work session — the only kind whose transcript can be
    re-read for token backfill. Synthetic ids (\"\", drive-*) stay honest
    zeros; they never had a transcript."""
    global _UUID_RE
    if _UUID_RE is None:
        import re
        _UUID_RE = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    return bool(sid) and bool(_UUID_RE.fullmatch(sid.strip()))


def _backfill_session_tokens(
    scores_db: str, sess: dict, source_path: str, override_dir: str,
) -> None:
    """Re-attribute a zero-token UUID session from its transcript (the fix
    for write-time-only reads in _record_agent_run: an unreadable-then
    transcript stamped tokens=0 forever). Buckets per-turn transcript events
    into each step row's [started_at, next-start/ended_at) window — trying
    hour-aligned shifts to correct known tz-skewed stamps — and PERSISTS
    repaired rows keyed (run_id, agent_id, step) so the repair runs once,
    not per-request. Never invents: only events landing inside this drive's
    own step windows count; a missing/empty/unalignable transcript leaves
    everything at 0 (the UI renders an em-dash)."""
    try:
        from prism_service.services.claude_transcripts import (
            live_token_events_for_session,
        )
        events = live_token_events_for_session(
            sess["session_id"], source_path, override_dir=override_dir or None)
    except Exception:
        return
    if not events:
        return
    steps = sess["steps"]
    starts = [_ts_epoch(st.get("_started_at")) for st in steps]
    ends = [_ts_epoch(st.get("_ended_at")) for st in steps]
    # Window k: [start_k, start_{k+1}); the last window falls back to its own
    # ended_at (or +inf) — bounded to THIS session's recorded step span so a
    # transcript that later worked other tasks can't be vacuumed in.
    windows: list[tuple[float, float] | None] = []
    for i, s in enumerate(starts):
        if s is None:
            windows.append(None)
            continue
        nxt = next((x for x in starts[i + 1:] if x is not None), None)
        end = nxt if nxt is not None else (ends[i] if ends[i] is not None else float("inf"))
        windows.append((s, max(s, end)))

    def _claim(shift_s: float) -> list[int]:
        claimed = [0] * len(steps)
        for ev_ts, tok in events:
            ts = ev_ts - shift_s
            for i, w in enumerate(windows):
                if w and w[0] <= ts < w[1]:
                    claimed[i] += int(tok or 0)
                    break
        return claimed

    # agent_runs stamps have shipped with whole-hour timezone skew vs the
    # transcript's UTC timestamps (observed: a drive whose windows sat 5h off
    # every event). Try hour-aligned shifts, nearest first, and keep the first
    # alignment that claims anything — only ever counting events that land in
    # THIS drive's own step windows. No alignment -> honest zeros; there is
    # deliberately NO whole-transcript fallback (it vacuumed a 4-day session's
    # 730M tokens onto a 10-minute drive — the ticket's likely_misfire).
    claimed = [0] * len(steps)
    for h in sorted(range(-12, 13), key=abs):
        c = _claim(h * 3600.0)
        if sum(c) > 0:
            claimed = c
            break
    if sum(claimed) == 0:
        return
    conn = _connect(scores_db)
    try:
        for st, tok in zip(steps, claimed):
            if tok > 0:
                st["tokens"] = tok
                conn.execute(
                    "UPDATE agent_runs SET tokens = ? "
                    "WHERE run_id = ? AND agent_id = ? AND step = ?",
                    (tok, st["_run_id"], st["_agent_id"], st["step"]),
                )
        conn.commit()
    finally:
        conn.close()
    sess["tokens_total"] = sum(st["tokens"] for st in steps)


def build_task_trace(
    scores_db: str, task_id: str, source_path: str = "", override_dir: str = "",
) -> dict:
    """Drive-scoped token trace for the task-detail Trace tab: the task's
    agent_runs grouped session -> SDLC step, token counts on every row.

    Shape: ``{"sessions": [{session_id, tokens_total,
    steps: [{step, role, model, tokens, gate_state, gate_source, ts}]}],
    "totals": {tokens, steps, sessions}}``. Sessions appear in first-activity
    order; steps within a session stay time-ordered. Zero rows returns empty
    arrays so the tab renders an honest empty state (cross-task totals live on
    the Sessions page, not here).

    When ``source_path``/``override_dir`` are given, a real (UUID) session
    whose rows all read 0 tokens is re-attributed from its live transcript
    (see _backfill_session_tokens) and totals are recomputed AFTER backfill —
    the sum-of-sessions == totals.tokens invariant always holds."""
    empty = {"sessions": [], "totals": {"tokens": 0, "steps": 0, "sessions": 0}}
    if not Path(scores_db).exists():
        return empty
    conn = _connect(scores_db)
    try:
        rows = conn.execute(
            "SELECT run_id, agent_id, session_id, step, role, model, tokens, "
            "       gate_state, duration_ms, started_at, ended_at, "
            "       recorded_at "
            "FROM agent_runs WHERE task_id = ? "
            "ORDER BY started_at ASC, recorded_at ASC",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return empty
    sessions: list[dict] = []
    by_sid: dict[str, dict] = {}
    for r in rows:
        sid = r["session_id"] or ""
        tok = int(r["tokens"] or 0)
        sess = by_sid.get(sid)
        if sess is None:
            sess = {"session_id": sid, "tokens_total": 0, "steps": []}
            by_sid[sid] = sess
            sessions.append(sess)
        sess["tokens_total"] += tok
        sess["steps"].append({
            "step": r["step"],
            "role": r["role"],
            "model": r["model"],
            "tokens": tok,
            "gate_state": r["gate_state"],
            "gate_source": gate_source_for_row(dict(r)),
            "ts": r["started_at"] or r["recorded_at"],
            # Private keys for the backfill (stripped before return): the
            # UPDATE needs the upsert triple + the row's time window.
            "_run_id": r["run_id"],
            "_agent_id": r["agent_id"],
            "_started_at": r["started_at"],
            "_ended_at": r["ended_at"],
        })
    if source_path or override_dir:
        for sess in sessions:
            if sess["tokens_total"] == 0 and _is_real_session(sess["session_id"]):
                _backfill_session_tokens(scores_db, sess, source_path, override_dir)
    for sess in sessions:
        for st in sess["steps"]:
            st.pop("_run_id", None)
            st.pop("_agent_id", None)
            st.pop("_started_at", None)
            st.pop("_ended_at", None)
    # Totals AFTER backfill — sum of session totals, so the invariant
    # sum(sessions[].tokens_total) == totals.tokens holds by construction.
    return {
        "sessions": sessions,
        "totals": {
            "tokens": sum(s["tokens_total"] for s in sessions),
            "steps": len(rows),
            "sessions": len(sessions),
        },
    }


def get_agent_run_aggregates(scores_db: str) -> dict:
    """Cross-run aggregates for the /learning panel: avg duration per step,
    override rate (gate steps that ended override/blind), token cost per
    role. Returns empty lists when there is no data."""
    if not Path(scores_db).exists():
        return {"per_step": [], "per_role": [], "override_rate": 0.0,
                "total_runs": 0}
    conn = _connect(scores_db)
    try:
        per_step = [dict(r) for r in conn.execute(
            "SELECT step, COUNT(*) AS n, AVG(duration_ms) AS avg_duration_ms, "
            "       AVG(tokens) AS avg_tokens "
            "FROM agent_runs GROUP BY step ORDER BY n DESC"
        ).fetchall()]
        per_role = [dict(r) for r in conn.execute(
            "SELECT role, COUNT(*) AS n, SUM(tokens) AS total_tokens, "
            "       AVG(tokens) AS avg_tokens "
            "FROM agent_runs GROUP BY role ORDER BY total_tokens DESC"
        ).fetchall()]
        total = conn.execute(
            "SELECT COUNT(*) FROM agent_runs").fetchone()[0] or 0
        # Override rate: rows whose verdict mentions override/blind (the
        # recurring structurally-blind-verifier recovery) over all rows.
        overrides = conn.execute(
            "SELECT COUNT(*) FROM agent_runs "
            "WHERE LOWER(COALESCE(verdict_summary,'')) LIKE '%override%' "
            "   OR LOWER(COALESCE(verdict_summary,'')) LIKE '%blind%'"
        ).fetchone()[0] or 0
    finally:
        conn.close()
    return {
        "per_step": per_step,
        "per_role": per_role,
        "override_rate": (overrides / total) if total else 0.0,
        "total_runs": total,
    }
