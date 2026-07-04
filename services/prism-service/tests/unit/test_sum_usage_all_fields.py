"""v6.7.23 — token attribution counts ALL FOUR usage fields.

Every token surface used to sum usage.output_tokens ONLY. Across 36 real
transcripts that displayed 15.9M vs the real 2.97B spend (output 15.9M +
input 1.6M + cache_read 2.88B + cache_creation 77.2M) — a 187x undercount.
These tests pin the fix: claude_transcripts.sum_usage is the ONE summer
(output + input + cache_read + cache_creation, nested cache_creation
breakdown as fallback only) and both the post-hoc import path
(parse_session_metrics -> session_outcomes.tokens_used) and the live
conductor read (live_tokens_for_session) route through it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from prism_service.services import claude_transcripts as ct  # noqa: E402

# One real-shaped usage dict (mirrors Claude Code JSONL on disk): both the
# flat cache_creation_input_tokens AND the nested cache_creation breakdown
# are present, and the flat value equals the nested sum.
_FULL_USAGE = {
    "input_tokens": 100,
    "output_tokens": 7,
    "cache_read_input_tokens": 5000,
    "cache_creation_input_tokens": 300,
    "cache_creation": {
        "ephemeral_1h_input_tokens": 300,
        "ephemeral_5m_input_tokens": 0,
    },
    "server_tool_use": {"web_search_requests": 0},
}
_FULL_TOTAL = 100 + 7 + 5000 + 300


def test_sum_usage_counts_all_four_fields():
    assert ct.sum_usage(_FULL_USAGE) == _FULL_TOTAL


def test_sum_usage_never_double_counts_nested_cache_creation():
    """Flat cache_creation_input_tokens is authoritative; the nested
    cache_creation breakdown (which equals it in real transcripts) must
    NOT be added on top."""
    comps = ct.usage_components(_FULL_USAGE)
    assert comps["cache_creation_input_tokens"] == 300
    assert ct.sum_usage(_FULL_USAGE) == _FULL_TOTAL  # not +300 again


def test_sum_usage_nested_cache_creation_fallback():
    """When ONLY the nested cache_creation shape exists, its values sum."""
    u = {
        "output_tokens": 1,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 40,
            "ephemeral_5m_input_tokens": 2,
        },
    }
    assert ct.sum_usage(u) == 43


def test_sum_usage_defaults_zero():
    assert ct.sum_usage({}) == 0
    assert ct.sum_usage(None) == 0
    assert ct.sum_usage({"output_tokens": 9}) == 9  # output-only still works


def _write_transcript(path: Path, sid: str, usages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, u in enumerate(usages):
        lines.append(json.dumps({
            "sessionId": sid,
            "type": "assistant",
            "timestamp": f"2026-07-03T10:00:{i:02d}Z",
            "message": {"role": "assistant", "usage": u},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_live_tokens_for_session_includes_cache_tokens(tmp_path):
    """The live conductor read (override_dir path) must report the FULL
    per-turn spend, cache reads included — not just output_tokens."""
    sid = "sess-live-total"
    d = tmp_path / "transcripts"
    _write_transcript(d / f"{sid}.jsonl", sid, [_FULL_USAGE, _FULL_USAGE])
    total = ct.live_tokens_for_session(sid, "", override_dir=str(d))
    assert total == 2 * _FULL_TOTAL
    # Output-only would have been 14 — the 187x-undercount regression.
    assert total != 14


def test_parse_session_metrics_tokens_used_includes_all_fields(tmp_path):
    """session_outcomes.tokens_used (post-hoc import path) sums all four
    usage fields per assistant turn."""
    sid = "sess-import-total"
    p = tmp_path / f"{sid}.jsonl"
    _write_transcript(p, sid, [_FULL_USAGE])
    metrics = ct.parse_session_metrics(p)
    assert metrics is not None
    assert metrics["tokens_used"] == _FULL_TOTAL


def test_token_events_carry_full_turn_tokens(tmp_path):
    """The per-turn timeline (_token_events -> live_token_events) attributes
    the full sum_usage total to each turn."""
    sid = "sess-events-total"
    d = tmp_path / "t"
    _write_transcript(d / f"{sid}.jsonl", sid, [_FULL_USAGE])
    events = ct.live_token_events_for_session(sid, "", override_dir=str(d))
    assert [tok for _, tok in events] == [_FULL_TOTAL]
