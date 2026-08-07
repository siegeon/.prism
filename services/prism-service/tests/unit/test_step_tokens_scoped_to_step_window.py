"""Unit — per-step token totals on the StepRail must be WINDOWED to each
step's own [entry, exit) span.

StepRail.tsx used to sum every history row's `turn_tokens` (itself the
tokens in (prev_row_ts, this_row] per api.tasks._attach_turn_tokens) bucketed
by workflow step. The row that ADVANCES INTO a step absorbs whatever gap
preceded it — a task idle for hours before starting the workflow read
"review previous notes 7.5M tok · 49s" (~153k tok/s, impossible) because the
advance-in row's turn_tokens carried the whole idle gap, not the step's own
49 seconds of work.

Fix: api.tasks._step_token_totals walks this task's own advance_task history
to find each step's real [entry, exit) span and sums the token-EVENT stream
via claude_transcripts.tokens_in_window bounded to just that span — reusing
the same function phase_progress uses for the CURRENT step, so a step's
number can never include tokens spent before it began.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_BASE = "2026-08-04T19:"


def _iso(mm: int, ss: int) -> str:
    return f"{_BASE}{mm:02d}:{ss:02d}+00:00"


def _write_transcript(path: Path, events: list[tuple[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "timestamp": ts,
            "message": {"usage": {"output_tokens": tok}},
        })
        for ts, tok in events
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _history():
    """Mirrors task 0784729f's real rows: idle for hours, then advances
    review_previous_notes -> draft_story (49s) -> story_gate."""
    return [
        {"action": "created", "timestamp": "2026-08-04T04:00:00+00:00"},
        {"action": "advance_task",
         "details": "from=<start>; to=review_previous_notes",
         "timestamp": _iso(39, 43)},
        {"action": "advance_task",
         "details": "from=review_previous_notes; to=draft_story",
         "timestamp": _iso(40, 32)},
        {"action": "advance_task",
         "details": "from=draft_story; to=story_gate",
         "timestamp": _iso(41, 40)},
    ]


def test_step_totals_exclude_the_pre_entry_idle_burst(tmp_path, monkeypatch):
    from prism_service.services import claude_transcripts as ct
    from prism_service.services.claude_transcripts import path_to_slug
    from prism_service.api import tasks as T

    sess = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    source_path = str(tmp_path / "src" / "proj")
    claude_home = tmp_path / "claude"
    proj = claude_home / "projects" / path_to_slug(source_path)
    # A 6.6M-token burst hours BEFORE the task entered the workflow, then the
    # step's REAL work: 50k tokens strictly inside review_previous_notes'
    # 49s window, 30k strictly inside draft_story's window.
    _write_transcript(proj / f"{sess}.jsonl", [
        ("2026-08-04T16:37:00+00:00", 6_600_000),
        (_iso(40, 0), 50_000),
        (_iso(41, 0), 30_000),
    ])
    monkeypatch.setattr(ct, "resolve_claude_home", lambda: claude_home)
    monkeypatch.setattr(ct, "_project_source_path", lambda _p: source_path)

    totals = T._step_token_totals(
        _history(), sessions=[{"session_id": sess}], project="proj")

    assert totals.get("review_previous_notes") == 50_000, totals
    assert totals.get("draft_story") == 30_000, totals
    assert not totals.get("story_gate"), totals


def test_step_totals_empty_without_a_linked_transcript(tmp_path, monkeypatch):
    """No linked session -> {} (never borrow the project-wide wall-clock
    fallback for per-step figures — that is exactly the cross-task-pollution
    risk that would reintroduce the same misattribution). Render nothing
    rather than a wrong number."""
    from prism_service.services import claude_transcripts as ct
    from prism_service.api import tasks as T

    monkeypatch.setattr(ct, "_project_source_path", lambda _p: str(tmp_path / "src"))
    totals = T._step_token_totals(_history(), sessions=[], project="proj")
    assert totals == {}, totals
