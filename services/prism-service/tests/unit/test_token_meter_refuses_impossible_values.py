"""RED -> GREEN suite for task fc471aed: agent_runs must never persist an
impossible token count -- a value bigger than the context window of the
model that (claimed to have) produced it.

Drives the real upsert_agent_run/get_agent_runs data-access functions
against a real sqlite scores.db on disk -- the SAME functions the HTTP
ingest route (api/agent_runs.py) and the conductor's own in-process
writer (conductor_service._record_agent_run, a POLICY_FILE this task
never edits) both funnel through
(services/prism-service/prism_service/services/agent_runs_data.py).

Measured live on 2026-08-30 against scores.db: a single row recorded
2,659,518,144 tokens with model=None and cost_usd=None -- thousands of
times larger than any model's context window. This suite pins the fix:
refuse an impossible value by name at write time, never clamp it, never
touch historical rows, never make the ingest path so strict a real run
stops recording.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# IMPOSSIBLE_TOKENS_REFUSAL does not exist on disk yet at the RED commit --
# importing it at module scope would abort collection of this whole file
# with rc=2 (a collection error) instead of letting each assertion run and
# fail with rc=1 (real red). get_agent_runs/upsert_agent_run already exist,
# so those stay at module scope; IMPOSSIBLE_TOKENS_REFUSAL is imported
# lazily, inside each test that needs it.
from prism_service.services.agent_runs_data import get_agent_runs, upsert_agent_run


def _row(**kw) -> dict:
    base = dict(
        run_id="run-1", workflow_name="conductor", task_id="T-1",
        session_id="S-1", agent_id="agent-1", parent_agent_id=None,
        role="sm", step="plan_gate", model="claude-sonnet-5",
        started_at="2026-08-30T00:00:00+00:00",
        ended_at="2026-08-30T00:01:00+00:00", duration_ms=60000,
        tokens=1000, tool_uses=1, ok=True, gate_state="none",
        verdict_summary=None, evidence_ref=None, cost_usd=None,
    )
    base.update(kw)
    return base


# ----------------------------------------------------------------------
# AC-1: tokens above the RECORDED MODEL's own context window are refused.
# ----------------------------------------------------------------------


def test_upsert_refuses_tokens_above_model_context_window(tmp_path):
    from prism_service.services.agent_runs_data import IMPOSSIBLE_TOKENS_REFUSAL

    db = str(tmp_path / "scores.db")
    result = upsert_agent_run(
        db, _row(run_id="run-impossible-1", model="claude-sonnet-5",
                  tokens=999_999_999),
    )
    assert result["ok"] is False, result
    assert result["refused"] == IMPOSSIBLE_TOKENS_REFUSAL, result

    rows = [r for r in get_agent_runs(db) if r["run_id"] == "run-impossible-1"]
    assert not rows, (
        f"an impossible tokens value must never reach the table: {rows}"
    )


# ----------------------------------------------------------------------
# AC-2: no model recorded -> checked against the GLOBAL max, never
# unlimited. Uses the exact bogus value measured live on 2026-08-30, with
# cost_usd set so this row carries a real LLM-turn signature (task
# c740443e's existing zeroing guard is deliberately NOT what is under
# test here -- see test_zero_without_llm_turn_signature_still_zeroes).
# ----------------------------------------------------------------------


def test_upsert_refuses_tokens_above_global_ceiling_when_model_unknown(tmp_path):
    from prism_service.services.agent_runs_data import IMPOSSIBLE_TOKENS_REFUSAL

    db = str(tmp_path / "scores.db")
    result = upsert_agent_run(
        db, _row(run_id="run-impossible-2", model=None, cost_usd=0.05,
                  duration_ms=142260292, tokens=2_659_518_144),
    )
    assert result["ok"] is False, result
    assert result["refused"] == IMPOSSIBLE_TOKENS_REFUSAL, result

    rows = [r for r in get_agent_runs(db) if r["run_id"] == "run-impossible-2"]
    assert not rows, (
        f"a model-less row above the global ceiling must not persist: {rows}"
    )


# ----------------------------------------------------------------------
# AC-3: a plausible, real token count for a known model writes through
# unchanged -- the guard never blocks a legitimate run.
# ----------------------------------------------------------------------


def test_upsert_keeps_plausible_tokens_for_known_model(tmp_path):
    db = str(tmp_path / "scores.db")
    result = upsert_agent_run(
        db, _row(run_id="run-real-1", model="claude-opus-5", tokens=50_000),
    )
    assert result["ok"] is True, result

    rows = [r for r in get_agent_runs(db) if r["run_id"] == "run-real-1"]
    assert len(rows) == 1, rows
    assert int(rows[0]["tokens"]) == 50_000, rows[0]


# ----------------------------------------------------------------------
# AC-4: the refusal is a stable, greppable, NAMED signal -- distinct from
# an ordinary successful write.
# ----------------------------------------------------------------------


def test_impossible_value_refusal_is_named_not_silent(tmp_path):
    db = str(tmp_path / "scores.db")
    ok_result = upsert_agent_run(
        db, _row(run_id="run-ok", model="claude-opus-5", tokens=1000),
    )
    assert ok_result.get("refused") is None, ok_result

    refused_result = upsert_agent_run(
        db, _row(run_id="run-refused", model="claude-sonnet-5",
                  tokens=5_000_000_000),
    )
    assert refused_result["ok"] is False, refused_result
    assert refused_result["refused"] == "impossible_token_count", refused_result
    assert refused_result.get("reason"), refused_result


# ----------------------------------------------------------------------
# AC-5: existing historical rows (including one shaped exactly like the
# real bogus data) are never rewritten, deleted, or migrated by this
# slice -- only the write path changes, never what is already on disk.
# ----------------------------------------------------------------------


def test_historical_rows_are_never_touched(tmp_path):
    db = str(tmp_path / "scores.db")
    # Prime the schema via the normal writer, then seed a bogus row
    # DIRECTLY -- bypassing the new guard, the same way the real
    # historical data got onto disk (written before this guard existed).
    upsert_agent_run(db, _row(run_id="seed", model="claude-opus-5", tokens=1))
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO agent_runs (run_id, agent_id, step, model, "
            "tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
            ("run-historical-bogus", "agent-old", "plan_gate", None,
             2_659_518_144, None),
        )
        conn.commit()
    finally:
        conn.close()

    # A normal, unrelated write for a DIFFERENT run_id must not touch it.
    upsert_agent_run(
        db, _row(run_id="run-unrelated", model="claude-opus-5", tokens=2000),
    )

    rows = [r for r in get_agent_runs(db, limit=2000)
            if r["run_id"] == "run-historical-bogus"]
    assert len(rows) == 1, rows
    assert int(rows[0]["tokens"]) == 2_659_518_144, (
        f"a pre-existing historical row must never be rewritten: {rows[0]}"
    )


# ----------------------------------------------------------------------
# AC-6: the pre-existing zero-without-signature guard (task c740443e) is
# unchanged -- a non-LLM bookkeeping row (no model, no measured duration)
# still gets tokens forced to 0, exactly as before this slice.
# ----------------------------------------------------------------------


def test_zero_without_llm_turn_signature_still_zeroes(tmp_path):
    db = str(tmp_path / "scores.db")
    result = upsert_agent_run(
        db, _row(run_id="run-bookkeeping", model=None, duration_ms=0,
                  cost_usd=None, tokens=500),
    )
    assert result["ok"] is True, result
    rows = [r for r in get_agent_runs(db) if r["run_id"] == "run-bookkeeping"]
    assert len(rows) == 1, rows
    assert int(rows[0]["tokens"] or 0) == 0, rows[0]
