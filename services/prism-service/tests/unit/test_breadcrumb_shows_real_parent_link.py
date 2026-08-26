"""The task page's top breadcrumb must reflect the task's REAL parent_id,
not just browser navigation history (owner, live remote-assist session:
"i see no way to see the parent to navigate to the parent").

Before this fix, TaskDetailPage.tsx's breadcrumb root button navigated to
`from` (browser `location.state`) and only LABELLED itself "Parent" when
`from` happened to start with "/tasks/" -- true only when the page was
reached by clicking down from its own parent's detail view. A child task
opened directly (URL, bookmark, refresh -- exactly how a driving session's
remote-assist screenshots reach it) showed "Tasks" at the top with no
visible parent affordance at all. The ONLY real, `task.parent_id`-driven
link lived in a separate Card far down the page, past the entire SDLC
trace and gate-decision detail -- easy to miss, and invisible without
scrolling past everything else.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned
by asserting the ACTUAL TSX source (tests/unit/test_conductor_page_
animated_cleanup_ui.py:4-6). Comments are stripped before every assertion
(repo convention, CLAUDE.md Lessons e139295d) so a comment can never
satisfy a guard the real code is supposed to own.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_TSX = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
       / "TaskDetailPage.tsx")

_BREADCRUMB_MARKER = (
    '<div className="flex items-center gap-1.5 text-xs '
    'text-[color:var(--text-muted)]">')


def _read() -> str:
    assert _TSX.exists(), f"{_TSX} missing"
    return _TSX.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments so a comment can never satisfy
    a source assertion."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


_BREADCRUMB_END_MARKER = "<AnimatePresence>"


def _breadcrumb_block() -> str:
    """The breadcrumb div is immediately followed by the page's
    `<AnimatePresence>` notice block (a unique, unambiguous next-section
    marker) -- sliced from the breadcrumb's own opening tag up to that
    marker, searched STARTING AFTER the breadcrumb marker so an earlier,
    unrelated <AnimatePresence> elsewhere in the file can't be matched."""
    src = _strip_comments(_read())
    start = src.index(_BREADCRUMB_MARKER)
    end = src.index(_BREADCRUMB_END_MARKER, start)
    return src[start:end]


def test_breadcrumb_click_navigates_to_the_real_parent_id():
    block = _breadcrumb_block()
    assert "task?.parent_id" in block or "task.parent_id" in block, (
        "the breadcrumb's onClick must branch on the task's REAL "
        "parent_id, not only on browser navigation state (`from`)"
    )
    assert "/tasks/${task.parent_id}" in block, (
        "when a real parent exists, the breadcrumb must navigate to it "
        "directly (/tasks/<parent_id>), not merely to wherever `from` "
        "happens to point"
    )


def test_breadcrumb_label_says_parent_whenever_a_real_parent_exists():
    block = _breadcrumb_block()
    # The label logic must be gated on parent_id, not solely derived from
    # `from.startsWith("/tasks/")` -- that alone is the exact bug this
    # test guards against (true only via session navigation history, false
    # for a task opened by direct URL/bookmark/refresh with a real parent).
    assert re.search(r"task\??\.parent_id\s*\?\s*[\"']Parent[\"']", block), (
        "the crumb label must render \"Parent\" whenever task.parent_id "
        "is set, independent of how the page was navigated to"
    )


def test_a_root_task_with_no_parent_keeps_the_old_context_aware_back_nav():
    """The fix must not regress a ROOT task's (no parent_id) breadcrumb --
    it should still fall back to the existing from-based back navigation
    (Tasks/Conductor/Parent-by-history), unchanged."""
    block = _breadcrumb_block()
    assert 'from.startsWith("/tasks/")' in block, (
        "a task with no real parent must still fall back to the "
        "pre-existing from-based back-navigation logic"
    )
    assert 'navigate(from)' in block, (
        "the fallback path (no real parent_id) must still call "
        "navigate(from), preserving today's context-aware back button "
        "for root tasks"
    )
