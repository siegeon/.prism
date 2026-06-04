"""Unit guard for the conductor burn-graph rate math.

live_token_turns_for_session derives per-turn tok_s = output_tokens / dt, where
dt is the wall-clock gap between consecutive transcript events. Those events are
message WRITE times, not generation durations, so a rapid tool-result ->
assistant pair can land far less than a second apart. Without a floor, out/dt
explodes into a megatoken/s phantom (the observed "peak 1369.1k tok/s" spike
that flatlines every other bar on the tile). dt must floor at 1s.
"""

from __future__ import annotations

from pathlib import Path
import sys

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import claude_transcripts as ct


def test_subsecond_gap_does_not_produce_phantom_rate(monkeypatch):
    # Two events 0.01s apart; the 2nd turn emits 1500 output tokens.
    raw = [(1000.0, 0), (1000.01, 1500)]
    monkeypatch.setattr(ct, "live_token_events_for_session", lambda *a, **k: raw)

    turns = ct.live_token_turns_for_session("sess", "C:/proj")
    assert turns, turns
    last = turns[-1]
    # dt floored to >= 1s, so tok_s is out/1.0 = 1500, NOT out/0.01 = 150000.
    assert last["dt_s"] >= 1.0, last
    assert last["tok_s"] <= 1500.0, last
    # No turn may report a physically impossible burn rate.
    assert all(t["tok_s"] <= t["out"] for t in turns), turns


def test_idle_gap_over_ceiling_also_clamps(monkeypatch):
    # A 20-minute idle gap must not read as a near-zero phantom rate either.
    raw = [(1000.0, 0), (1000.0 + 1200.0, 600)]
    monkeypatch.setattr(ct, "live_token_events_for_session", lambda *a, **k: raw)

    turns = ct.live_token_turns_for_session("sess", "C:/proj")
    last = turns[-1]
    assert last["dt_s"] == 1.0, last  # >600s gap clamps to the 1s window
    assert last["tok_s"] == 600.0, last
