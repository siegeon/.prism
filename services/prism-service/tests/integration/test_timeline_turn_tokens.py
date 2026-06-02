"""Regression — task-detail Timeline per-turn token pills (v6.3.13).

Two bugs left every task's timeline showing NO per-turn token pills:

  Bug A (shape) — TaskService.history() yields TaskHistory DATACLASSES, but
    api/tasks._attach_turn_tokens assumed dicts (h.get / h[...] / isinstance
    dict). It AttributeError'd on the first row, the swallow-all except hid
    it, and turn_tokens never reached the JSON. Fix: get_task converts history
    rows to dicts before attribution.
  Bug B (linkage) — attribution only ran off task_sessions, which is empty for
    most tasks, so even with the shape fixed only link_session'd tasks showed
    tokens. Fix: project_token_events_in_window time-window fallback, bounded
    to the turn span and idle-clamped at 600s.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# 2026-01-01T00:00:00Z .. as a fixed wall clock the test reasons about.
_BASE = "2026-01-01T00:"


def _iso(mm: int, ss: int) -> str:
    return f"{_BASE}{mm:02d}:{ss:02d}+00:00"


def _write_transcript(path: Path, sess: str, events: list[tuple[str, int]]) -> None:
    """One assistant event per (iso_ts, output_tokens) pair."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "sessionId": sess,
            "timestamp": ts,
            "message": {"role": "assistant", "usage": {"output_tokens": tok}},
        })
        for ts, tok in events
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fake_project(tmp_path: Path):
    """claude_home/projects/<slug>/<sess>.jsonl with timestamped turns.

    Events: C @00:00:30 (50), A @00:01:30 (100), B @00:29:50 (200).
    Returns (claude_home, source_path)."""
    from prism_service.services.claude_transcripts import path_to_slug

    source_path = str(tmp_path / "src" / "proj")
    claude_home = tmp_path / "claude"
    slug = path_to_slug(source_path)
    proj = claude_home / "projects" / slug
    sess = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _write_transcript(
        proj / f"{sess}.jsonl", sess,
        [(_iso(0, 30), 50), (_iso(1, 30), 100), (_iso(29, 50), 200)],
    )
    return claude_home, source_path


def test_window_helper_returns_events_in_span(tmp_path, monkeypatch):
    from prism_service.services import claude_transcripts as ct

    claude_home, source_path = _fake_project(tmp_path)
    monkeypatch.setattr(ct, "resolve_claude_home", lambda: claude_home)
    from datetime import datetime

    def ep(mm, ss):
        return datetime.fromisoformat(_iso(mm, ss)).timestamp()

    ev = ct.project_token_events_in_window(source_path, ep(0, 0), ep(30, 0))
    assert sorted(t for _, t in ev) == [50, 100, 200]
    # until <= since collapses to empty.
    assert ct.project_token_events_in_window(source_path, ep(5, 0), ep(5, 0)) == []


def _history():
    """Dict rows (post-conversion shape) — created, in_progress, done."""
    return [
        {"id": 1, "action": "created", "timestamp": _iso(0, 0)},
        {"id": 2, "action": "updated", "timestamp": _iso(1, 0)},
        {"id": 3, "action": "updated", "timestamp": _iso(30, 0)},
    ]


def test_attach_buckets_with_idle_clamp(tmp_path, monkeypatch):
    """Fallback attribution: event C (00:00:30) -> the in_progress turn (50);
    event B (00:29:50, 10s before done) -> done (200); event A (00:01:30, ~28m
    before done) is idle-clamped OUT. No linked sessions -> fallback path."""
    from prism_service.services import claude_transcripts as ct
    from prism_service.api import tasks as T

    claude_home, source_path = _fake_project(tmp_path)
    monkeypatch.setattr(ct, "resolve_claude_home", lambda: claude_home)
    monkeypatch.setattr(ct, "_project_source_path", lambda _p: source_path)

    rows = _history()
    T._attach_turn_tokens(rows, sessions=[], project="proj")
    assert rows[0].get("turn_tokens") is None      # created — nothing before it
    assert rows[1].get("turn_tokens") == 50         # in_progress turn
    assert rows[2].get("turn_tokens") == 200        # done; event A clamped out


def test_attach_is_safe_on_dataclass_rows(monkeypatch):
    """Documents Bug A: TaskHistory dataclasses have no .get/__setitem__, so
    the attach must no-op (not raise) on them — which is exactly why get_task
    converts to dicts first."""
    from prism_service.api import tasks as T
    from prism_service.services.task_service import TaskHistory

    row = TaskHistory(id=1, task_id="t", actor="", action="created",
                      details="", timestamp=_iso(0, 0))
    # Must not raise even though rows are the wrong shape.
    T._attach_turn_tokens([row, row], sessions=[], project="proj")
    assert not hasattr(row, "turn_tokens")
