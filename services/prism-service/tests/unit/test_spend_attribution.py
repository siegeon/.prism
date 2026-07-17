"""RED scaffold (task ff160371) — honest per-field, per-turn-model USD spend.

sum_usage()/usage_components() (claude_transcripts.py ~93-129) treat all four
usage fields as equally "tokens" — right for token-budget accounting, wrong
for DOLLARS: a cache_read token is ~0.1x the price of an input token, and an
output token is ~5x an input token. Flat-rate "spend" misrepresents real cost.
Background subagent transcripts (nested <sid>/**/*.jsonl) are also folded
into the parent session's total by live_tokens_for_session, so background
spend is invisible AS background.

This file pins the fix: PRICING (per-model, per-field $/token) + usd_cost
(prices each field at its own rate) + live_spend_for_session (main/background
split, priced PER TURN by that turn's own message.model). ALL FAIL today —
usd_cost/live_spend_for_session/PRICING don't exist yet.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import claude_transcripts as ct  # noqa: E402


def _write_transcript(path: Path, turns: list[tuple[str, dict]], sid: str = "") -> None:
    """Write a transcript JSONL: one assistant turn per (model, usage_dict)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (model, usage) in enumerate(turns):
        evt = {
            "type": "assistant",
            "timestamp": f"2026-07-17T12:00:{i:02d}.000Z",
            "message": {"model": model, "usage": usage},
        }
        if sid:
            evt["sessionId"] = sid
        lines.append(json.dumps(evt))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _components(**kw) -> dict:
    out = {f: 0 for f in ct.USAGE_TOKEN_FIELDS}
    out.update(kw)
    return out


_KNOWN_MODEL = "claude-opus-4-8"
_KNOWN_MODEL_B = "claude-haiku-4-5"


# ----------------------------------------------------------------------
# AC-1 — usd_cost prices EACH field at its OWN rate (not a flat rate).
# ----------------------------------------------------------------------

def test_usd_cost_prices_cache_read_cheaper_than_output():
    """Same token count, same known model: cache_read must cost strictly
    LESS than output — proves per-field (not flat) pricing."""
    output_only = _components(output_tokens=1000)
    cache_read_only = _components(cache_read_input_tokens=1000)

    cost_output = ct.usd_cost(output_only, _KNOWN_MODEL)
    cost_cache_read = ct.usd_cost(cache_read_only, _KNOWN_MODEL)

    assert cost_output > 0, "output tokens on a known model must have nonzero cost"
    assert cost_cache_read < cost_output, (
        f"cache_read ({cost_cache_read}) must be cheaper than the same token "
        f"count of output ({cost_output}) — flat per-token pricing is dishonest"
    )


def test_usd_cost_known_model_is_in_pricing_table():
    assert _KNOWN_MODEL in ct.PRICING, (
        "claude-opus-4-8 (seen in this project's real transcripts) must have "
        "a sourced PRICING entry"
    )
    assert _KNOWN_MODEL_B in ct.PRICING, (
        "claude-haiku-4-5 must have a sourced PRICING entry"
    )


# ----------------------------------------------------------------------
# AC-2 — live_spend_for_session splits MAIN vs BACKGROUND (never folded).
# ----------------------------------------------------------------------

def test_live_spend_for_session_splits_main_vs_background(tmp_path):
    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    d = tmp_path / "claudedir"
    _write_transcript(
        d / f"{sid}.jsonl",
        [(_KNOWN_MODEL, _components(output_tokens=1000))],
        sid=sid,
    )
    _write_transcript(
        d / sid / "child-session.jsonl",
        [(_KNOWN_MODEL, _components(output_tokens=2000))],
        sid=sid,
    )

    result = ct.live_spend_for_session(sid, "", override_dir=str(d))

    assert result["main"]["tokens"] == 1000, "main must count ONLY the top-level transcript"
    assert result["background"]["tokens"] == 2000, (
        "background must count ONLY the nested subagent transcript, "
        "never folded into main"
    )
    assert result["main"]["usd"] > 0
    assert result["background"]["usd"] > result["main"]["usd"], (
        "background (2000 output tok) must cost more than main (1000 output "
        "tok) at the same model rate"
    )
    assert result["total"]["tokens"] == 3000
    assert abs(result["total"]["usd"] - (result["main"]["usd"] + result["background"]["usd"])) < 1e-9


# ----------------------------------------------------------------------
# AC-3 — pricing is PER TURN by that turn's own model (a session can mix).
# ----------------------------------------------------------------------

def test_live_spend_for_session_prices_per_turn_model_not_blended(tmp_path):
    sid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    d = tmp_path / "claudedir"
    turn_a = _components(output_tokens=1000)
    turn_b = _components(output_tokens=1000)
    _write_transcript(
        d / f"{sid}.jsonl",
        [(_KNOWN_MODEL, turn_a), (_KNOWN_MODEL_B, turn_b)],
        sid=sid,
    )

    result = ct.live_spend_for_session(sid, "", override_dir=str(d))

    expected = ct.usd_cost(turn_a, _KNOWN_MODEL) + ct.usd_cost(turn_b, _KNOWN_MODEL_B)
    assert abs(result["main"]["usd"] - expected) < 1e-9, (
        "total must equal the SUM of each turn's own-model cost"
    )

    # A wrong "blend everything at the more expensive model" implementation
    # would overcharge — prove we are NOT doing that.
    wrong_blended = ct.usd_cost(_components(output_tokens=2000), _KNOWN_MODEL)
    assert result["main"]["usd"] < wrong_blended, (
        "mixed-model total must be below the wrong all-at-opus blended figure "
        "(haiku is cheaper than opus)"
    )


# ----------------------------------------------------------------------
# AC-4 — unrecognized model: default-priced (nonzero) AND flagged, never
# silently trusted as a sourced rate.
# ----------------------------------------------------------------------

def test_live_spend_for_session_flags_unknown_model_as_unpriced(tmp_path):
    sid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    d = tmp_path / "claudedir"
    _write_transcript(
        d / f"{sid}.jsonl",
        [("claude-nonexistent-9000", _components(output_tokens=500))],
        sid=sid,
    )

    result = ct.live_spend_for_session(sid, "", override_dir=str(d))

    assert result["priced"] is False, (
        "an unrecognized model turn must force priced=False on the total"
    )
    assert result["main"]["unpriced_tokens"] == 500, (
        "the unpriced turn's tokens must be counted into unpriced_tokens"
    )
    assert result["main"]["usd"] > 0, (
        "an unknown model must still get a clearly-labeled DEFAULT dollar "
        "figure, never silently zero"
    )
