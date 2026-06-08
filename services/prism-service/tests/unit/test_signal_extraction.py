"""Red scaffold — task c80fd9bb: learning loop mints REAL memories, not loop noise.

Pins the acceptance criteria against the REAL code seams (verified from source,
not invented contracts). These FAIL today because the machinery filter, the
actor-tagged typed extractors, the degenerate-loop collapse, and the bounded
per-cycle cost gate do not yet exist.

Seams under test (all real, read on disk):
  * claude_transcripts.parse_session_metrics() — JSONL walk that today builds a
    raw `pushbacks` bucket via _PUSHBACK_RE on EVERY user turn with no actor tag
    and no machinery filter (claude_transcripts.py:175-178). The rework must add
    actor tagging + a deterministic machinery allow/deny gate + typed extractors
    (user_correction / failure_fix / novel_success) and a degenerate-loop
    collapse (N identical retries -> ONE 'stuck' signal).
  * claude_transcripts.import_unseen() -> _enqueue_with_signals() — the disk
    bridge that turns a transcript into a consolidation_candidate. A
    machinery-ONLY session (loop ticks + Stop-hooks + identical retries, no user
    turns) must enqueue 0; a genuine-signal session must enqueue 1 typed
    candidate. INTEGRATION assertion, not a unit call — drives the real bridge
    end to end over a real sqlite scores.db, counted from a SEPARATE connection.
  * consolidation_data — a CHEAP deterministic per-cycle gate that runs BEFORE
    any LLM call: a hard MAX_CANDIDATES_PER_CYCLE cap + a tokens/cycle ceiling
    that select_cycle_candidates() enforces and returns within.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import claude_transcripts as ct  # noqa: E402
from prism_service.services import consolidation_data as cd  # noqa: E402


SID = "sig-extract-session-1"


def _evt(ts, role, content):
    return {"sessionId": SID, "timestamp": ts, "message": {"role": role, "content": content}}


def _write(tmp_path, events, name="transcript.jsonl"):
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return p


# Synthetic machinery: Stop-hook re-invocations, autonomous-loop ticks, and
# scheduler heartbeats. These literally contain pushback regex words ("stop",
# "wait", "no") today and so register as N false "pushbacks".
_MACHINERY_TURNS = [
    "work our github tasks",                       # Stop-hook directive
    "# Autonomous loop tick\nScheduleWakeup",      # loop re-invocation
    "no new input; waiting on scheduler heartbeat",  # heartbeat (no/wait)
    "Stop hook feedback: continue the autonomous loop",
]


def _machinery_events(n_ticks=4):
    out = []
    for i, txt in enumerate(_MACHINERY_TURNS[:n_ticks]):
        # actor is the Stop hook / scheduler — internally-simulated feedback,
        # NOT a genuine user. Carried on a 'role' the rework must tag as system.
        out.append(_evt(f"2026-06-07T12:0{i}:00Z", "user", txt))
    return out


# ---------------------------------------------------------------------------
# (1) Deterministic machinery filter — NO LLM. Stop-hook directives, loop
#     ticks, and scheduler heartbeats must be recognized as machinery so they
#     never register as pushbacks/corrections.
# ---------------------------------------------------------------------------
def test_is_machinery_turn_flags_loop_and_hook_noise():
    assert hasattr(ct, "is_machinery_turn"), (
        "claude_transcripts must expose a deterministic is_machinery_turn() "
        "gate that runs BEFORE any LLM call"
    )
    for txt in _MACHINERY_TURNS:
        assert ct.is_machinery_turn(txt) is True, f"machinery not flagged: {txt!r}"


def test_is_machinery_turn_passes_genuine_user_correction():
    genuine = "no, stop grepping — read consolidation_data.py before you edit it"
    assert ct.is_machinery_turn(genuine) is False, (
        "a genuine user correction must NOT be classified as machinery"
    )


# ---------------------------------------------------------------------------
# (2) Machinery-only session yields ZERO typed signals (replaces the raw
#     pushback counter). parse_session_metrics must expose typed_signals.
# ---------------------------------------------------------------------------
def test_parse_machinery_only_session_yields_no_typed_signals(tmp_path):
    out = ct.parse_session_metrics(_write(tmp_path, _machinery_events()))
    assert out is not None
    typed = out["signals"].get("typed_signals")
    assert typed is not None, (
        "signals bucket must carry a 'typed_signals' list (user_correction / "
        "failure_fix / novel_success) replacing the raw pushback counter"
    )
    assert typed == [] or len(typed) == 0, (
        f"machinery-only session must mint NO typed signal; got {typed}"
    )
    # And the legacy raw pushback bucket must not be inflated by machinery.
    assert len(out["signals"].get("pushbacks") or []) == 0, (
        "Stop-hook / loop-tick / heartbeat turns must not count as pushbacks"
    )


# ---------------------------------------------------------------------------
# (3) A genuine USER correction of a prior assistant action is typed as a
#     'user_correction' with an actionable tip; system/Stop-hook actors are
#     excluded from correction extraction (actor tagging).
# ---------------------------------------------------------------------------
def test_genuine_user_correction_is_typed(tmp_path):
    events = [
        _evt("2026-06-07T12:00:00Z", "assistant",
             [{"type": "text", "text": "I'll mock the DB in the test."}]),
        _evt("2026-06-07T12:00:05Z", "user",
             "no, stop mocking the database — integration tests must hit a real DB"),
    ]
    out = ct.parse_session_metrics(_write(tmp_path, events))
    typed = out["signals"].get("typed_signals") or []
    kinds = {t.get("kind") for t in typed if isinstance(t, dict)}
    assert "user_correction" in kinds, (
        f"a genuine user correction must yield a user_correction signal; got {typed}"
    )
    corr = next(t for t in typed if t.get("kind") == "user_correction")
    assert corr.get("tip"), "each typed signal must carry an actionable 'tip'"


# ---------------------------------------------------------------------------
# (4) Reflexion degenerate-loop collapse: N IDENTICAL retries collapse to ONE
#     'stuck' signal, not N pushbacks.
# ---------------------------------------------------------------------------
def test_identical_retries_collapse_to_single_stuck_signal(tmp_path):
    same = [{"type": "tool_use", "id": "t", "name": "Bash",
             "input": {"command": "pytest -q"}}]
    err = [{"type": "tool_result", "tool_use_id": "t", "is_error": True,
            "content": "1 failed"}]
    events = []
    for i in range(5):  # five identical fail-retry cycles, no progress
        events.append(_evt(f"2026-06-07T12:0{i}:00Z", "assistant", same))
        events.append(_evt(f"2026-06-07T12:0{i}:30Z", "user", err))
    out = ct.parse_session_metrics(_write(tmp_path, events))
    typed = out["signals"].get("typed_signals") or []
    stuck = [t for t in typed if isinstance(t, dict) and t.get("kind") == "stuck"]
    assert len(stuck) == 1, (
        f"5 identical no-progress retries must collapse to ONE 'stuck' signal, "
        f"not N; got {len(stuck)} stuck / typed={typed}"
    )


# ---------------------------------------------------------------------------
# (5) INTEGRATION: drive the REAL disk bridge end to end over a real scores.db.
#     Machinery-only session -> 0 candidates enqueued. Genuine-signal session
#     -> 1 candidate. Counted from a SEPARATE connection (durability, not an
#     in-memory illusion). This is the user-facing seam (import_unseen ->
#     _enqueue_with_signals -> consolidation_candidates), not a unit call.
# ---------------------------------------------------------------------------
def _bootstrap_scores_db(tmp_path) -> str:
    from prism_service.engines.brain_engine import Brain
    scores_db = str(tmp_path / "scores.db")
    Brain(brain_db=str(tmp_path / "brain.db"),
          graph_db=str(tmp_path / "graph.db"),
          scores_db=scores_db)
    return scores_db


def _claude_home_with(tmp_path, events, sid_dir="proj"):
    project_path = tmp_path / sid_dir
    project_path.mkdir()
    slug = ct.path_to_slug(str(project_path))
    proj_dir = tmp_path / "claude" / "projects" / slug
    proj_dir.mkdir(parents=True)
    with (proj_dir / "t.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return tmp_path / "claude", str(project_path)


def _pending(scores_db) -> int:
    conn = sqlite3.connect(scores_db)  # SEPARATE connection
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM consolidation_candidates "
            "WHERE status='pending'").fetchone()[0])
    finally:
        conn.close()


def test_machinery_only_session_enqueues_zero_candidates(tmp_path):
    scores_db = _bootstrap_scores_db(tmp_path)
    home, proj = _claude_home_with(tmp_path, _machinery_events())
    ct.import_unseen(scores_db, proj, claude_home=home)
    assert _pending(scores_db) == 0, (
        "a machinery-only session (loop ticks + Stop-hooks + identical retries, "
        "no user turns) must mint 0 consolidation candidates"
    )


def test_genuine_signal_session_enqueues_one_candidate(tmp_path):
    scores_db = _bootstrap_scores_db(tmp_path)
    events = [
        _evt("2026-06-07T12:00:00Z", "assistant",
             [{"type": "text", "text": "I'll mock the DB."}]),
        _evt("2026-06-07T12:00:05Z", "user",
             "no, stop mocking the database — tests must hit a real DB"),
    ]
    home, proj = _claude_home_with(tmp_path, events)
    ct.import_unseen(scores_db, proj, claude_home=home)
    assert _pending(scores_db) == 1, (
        "a session with a genuine user correction must mint exactly 1 candidate"
    )


# ---------------------------------------------------------------------------
# (6) BOUNDED COST: a cheap deterministic gate runs BEFORE any LLM call. A hard
#     per-cycle candidate cap AND a tokens/cycle ceiling are enforced and
#     asserted. select_cycle_candidates() must never return more than the cap
#     and must respect the token ceiling.
# ---------------------------------------------------------------------------
def _make_cand_db(tmp_path) -> str:
    scores_db = str(tmp_path / "scores.db")
    conn = sqlite3.connect(scores_db)
    try:
        conn.execute(
            "CREATE TABLE consolidation_candidates ("
            "  id TEXT PRIMARY KEY, task_id TEXT, session_id TEXT,"
            "  trigger TEXT, scope_json TEXT, status TEXT DEFAULT 'pending',"
            "  queued_at TEXT)")
        conn.commit()
    finally:
        conn.close()
    return scores_db


def _seed_cand(scores_db, sid, *, est_tokens=100):
    import uuid
    from datetime import datetime, timezone
    scope = {"signal_counts": {"pushbacks": 1}, "est_tokens": est_tokens}
    conn = sqlite3.connect(scores_db)
    try:
        conn.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, scope_json, status, queued_at) "
            "VALUES (?, NULL, ?, 'transcript_imported', ?, 'pending', ?)",
            (str(uuid.uuid4()), sid, json.dumps(scope),
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def test_max_candidates_per_cycle_constant_exists():
    assert hasattr(cd, "MAX_CANDIDATES_PER_CYCLE"), (
        "consolidation_data must define a hard MAX_CANDIDATES_PER_CYCLE cap"
    )
    assert isinstance(cd.MAX_CANDIDATES_PER_CYCLE, int)
    assert cd.MAX_CANDIDATES_PER_CYCLE > 0


def test_select_cycle_candidates_honors_candidate_cap(tmp_path):
    assert hasattr(cd, "select_cycle_candidates"), (
        "consolidation_data must expose select_cycle_candidates() — the cheap "
        "deterministic gate that runs BEFORE any LLM call"
    )
    scores_db = _make_cand_db(tmp_path)
    for i in range(20):
        _seed_cand(scores_db, f"s{i}", est_tokens=100)
    picked = cd.select_cycle_candidates(scores_db, max_candidates=3,
                                        token_ceiling=10_000)
    assert len(picked) <= 3, (
        f"hard per-cycle candidate cap breached: returned {len(picked)} > 3"
    )


def test_select_cycle_candidates_honors_token_ceiling(tmp_path):
    scores_db = _make_cand_db(tmp_path)
    for i in range(20):
        _seed_cand(scores_db, f"s{i}", est_tokens=1_000)
    # ceiling of 2500 tokens with 1000-token candidates => at most 2 fit.
    picked = cd.select_cycle_candidates(scores_db, max_candidates=100,
                                        token_ceiling=2_500)
    total = sum(
        json.loads(p.get("scope_json") or "{}").get("est_tokens", 0)
        if isinstance(p, dict) else 0
        for p in picked
    )
    assert total <= 2_500, (
        f"tokens/cycle ceiling breached: selected {total} tokens > 2500 ceiling"
    )
