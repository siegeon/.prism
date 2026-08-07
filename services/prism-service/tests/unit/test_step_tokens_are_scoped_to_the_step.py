"""The per-step token readout describes the step (owner 2026-08-06).

THE SYMPTOM the owner reported, on a real task page: "IMPLEMENT TASKS ·
78.03% · 7/12 · 411M tok" with per-step rows like "review previous notes ·
7.5M tok · 49s" and "plan gate · 160M tok · 49h 39m". 7.5M tokens in 49
seconds is ~153,000 tokens/second, which no model produces. The owner's
reaction: "this seems crazy".

THE CAUSE, read out of the source rather than guessed:
ConductorService.phase_progress (conductor_service.py:4425-4435) loops over
`sessions_for_task(task_id)` and does `tokens += max(outcome_tok, live_tok)`
where live_tok is that session's LIFETIME total. Its own comment at
:4478 admits it - "Task-total linked-session tokens" - while the field it
fills is named `tokens_since_step` and the SPA renders it beside the current
step. The duration was real; the number next to it described the whole
history of every session ever linked to the task.

Compounding it, the events are BILLABLE tokens (output + input + cache_read
+ cache_creation, _sum_billable_tokens at claude_transcripts.py:1162), and
cache_read re-counts the entire context every single turn. That is why the
figure is ~100x anything a person would recognise as work done.

This pins the window scoping. It does NOT change which token fields are
counted - that is a separate, visible product decision, called out in the
helper's docstring rather than made silently here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


from prism_service.services.claude_transcripts import tokens_in_window


def test_only_events_inside_the_step_window_count():
    """A step that began at t=200 is not charged for the turns before it."""
    events = [(100.0, 5_000), (150.0, 7_000), (200.0, 11.0), (300.0, 13_000)]
    events = [(t, int(v)) for t, v in events]
    assert tokens_in_window(events, 200.0) == 11 + 13_000
    # The whole history is still available when you ask for it.
    assert tokens_in_window(events, 0.0) == 5_000 + 7_000 + 11 + 13_000


def test_a_step_with_no_turns_reads_zero_not_the_task_total():
    """THE REPORTED BUG. Every event predates the step, so the step's honest
    readout is 0 - not the 411M lifetime total the old code returned."""
    events = [(100.0, 250_000), (150.0, 161_000)]
    assert tokens_in_window(events, 900.0) == 0


def test_an_empty_timeline_is_zero_never_an_error():
    assert tokens_in_window([], 0.0) == 0


def test_the_boundary_event_belongs_to_the_step():
    """An event exactly at the step boundary is inside it - a turn is
    attributed to the step it started, never dropped between the two."""
    assert tokens_in_window([(500.0, 42)], 500.0) == 42
