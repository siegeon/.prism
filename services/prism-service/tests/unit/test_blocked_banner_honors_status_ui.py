"""The header "Blocked because" banner must not render on an actively
working task.

DEFECT observed live: task 1bc0b316 had status="in_progress" (ship_worker
was actively rebasing/pushing/polling CI on it, hands-off, right after
7.13.136's fix landed) while task.blocked_reason still held a STALE
string from an earlier failed ship attempt -- and TaskDetailPage.tsx's
header-level "Blocked because" <Card> rendered on `task.blocked_reason`
alone, with no check on `task.status`. Owner: "if work is happening in
the system then the ticket should not say anything about blocked."

The Evidence-tab card two screens down (around L2537) already got this
right -- `task.status === "blocked" && task.blocked_reason` -- so the
header banner is brought in line with that same guard, not a new
pattern.

The PRISM SPA has no JS test runner, so this UI AC is pinned by asserting
the actual TSX source (same convention as
test_conductor_page_animated_cleanup_ui.py).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_TASK_DETAIL = (_HERE.parent.parent.parent / "prism_service" / "web" / "src"
               / "pages" / "TaskDetailPage.tsx")

_GUARDED = '{task.status === "blocked" && task.blocked_reason && ('
_UNGUARDED = "{task.blocked_reason && ("


def _src() -> str:
    return _TASK_DETAIL.read_text(encoding="utf-8")


def test_no_blocked_reason_render_is_missing_the_status_guard():
    src = _src()
    assert _UNGUARDED not in src, (
        "found a 'Blocked because' render gated on task.blocked_reason "
        "alone -- it must also check task.status === \"blocked\", or a "
        "stale blocked_reason renders as BLOCKED on an actively working "
        "task (owner: 'if work is happening in the system then the "
        "ticket should not say anything about blocked')")


def test_both_blocked_reason_cards_carry_the_status_guard():
    src = _src()
    count = src.count(_GUARDED)
    assert count >= 2, (
        f'expected both the header banner and the Evidence-tab card to use '
        f'task.status === "blocked" && task.blocked_reason, found {count} '
        f"occurrence(s)")
