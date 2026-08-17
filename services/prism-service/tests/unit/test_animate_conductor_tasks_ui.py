"""UI contract for the Tasks BOARD (ui-redesign epic 16777a76, ws5/ws6).

HISTORY: this file originally pinned the kanban board's motion wiring
(task a5e0d9f5 — FLIP cards, AnimatePresence columns). The owner-approved
direction artifact (1fab352f) replaced the kanban with a Jira-style grouped
table under a persistent LIVE bar, so those pins were superseded by design.
This version pins the NEW board's honest-liveness + table contract, same
source-assert pattern (no JS test runner) as the other *_ui.py contracts.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_TASKS = _SRC / "pages" / "TasksPage.tsx"
_LIVEBAR = _SRC / "components" / "LiveBar.tsx"
_APP = _SRC / "App.tsx"
_CSS = _SRC / "index.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# LIVE bar — real conductor signal, honest idle, never a frozen fake pulse.
# Lives in the app SHELL (components/LiveBar.tsx, mounted in App.tsx) so the
# pulse is visible on EVERY page, not just the board (ws5 oracle).
# ---------------------------------------------------------------------------

# SUPERSEDED 2026-08-12 (task 40c29b83): the /api/conductor/state fetch
# moved OUT of LiveBar.tsx into the shared lib/useConductorState.ts hook
# (FR-1/FR-3) -- LiveBar now reaches the endpoint only by IMPORTING the
# hook, so the literal check moves to the hook file and this test instead
# pins the import shape.
_HOOK = _SRC / "lib" / "useConductorState.ts"


def test_live_bar_reads_real_conductor_state():
    src = _read(_LIVEBAR)
    assert re.search(
        r'import\s*\{[^}]*\buseConductorState\b[^}]*\}\s*from\s*'
        r'["\']@/lib/useConductorState["\']',
        src,
    ), "LiveBar must import useConductorState (the shared conductor work-state hook)"
    assert _HOOK.exists(), f"expected {_HOOK} to own the endpoint fetch"
    hook_src = _read(_HOOK)
    assert "/api/conductor/state" in hook_src, \
        "useConductorState must read the conductor's real work-state endpoint"


def test_live_bar_is_mounted_in_the_app_shell():
    app = _read(_APP)
    assert "LiveBar" in app, \
        "LiveBar must mount in the shell so it persists on every page"


def test_live_bar_has_honest_idle_state():
    src = _read(_LIVEBAR)
    # The pulse may ONLY render when something is actually being driven;
    # a quiet queue shows an explicit idle message instead.
    assert "isLive" in src or "idle" in src.lower(), \
        "LiveBar needs an explicit idle state, not an always-on pulse"
    assert "animate-pulse" in src and "isLive" in src, \
        "the pulsing dot must be conditional on real liveness"


def test_reduced_motion_global_reset_covers_the_pulse():
    css = _read(_CSS)
    assert "prefers-reduced-motion" in css, \
        "index.css must carry the global reduced-motion reset"


# ---------------------------------------------------------------------------
# Grouped table — artifact information design on real rows
# ---------------------------------------------------------------------------

def test_board_rows_link_to_task_detail():
    src = _read(_TASKS)
    assert "/tasks/${" in src, \
        "row summaries must Link to /tasks/:id (dead text is the old misfire)"


def test_board_uses_lozenge_primitives_for_step_and_gate():
    src = _read(_TASKS)
    assert 'from "@/components/Lozenge"' in src, \
        "status/step/gate chips must route through the shared Lozenge"


# (retired) test_board_groups_by_status_buckets asserted the board renders
# "In progress" / "At a gate" / "Up next" / "Blocked" group headers. Commit
# ea72bb3 ("feat(ui): unified team work view across native/GitHub/Jira",
# task ae31c2c0) deliberately replaced that bucketed board with ONE unified
# work table whose every row carries its own status and gate column, so the
# headers no longer exist and this assertion had been red on main ever since.
# The current contract is pinned by tests/integration/test_unified_work_ui.py
# (My Work/Team toggle, source + assignee filters, provider badge and
# backlink on external rows) — 8 tests, green. Retired here rather than left
# standing, because a contradiction nobody owns is just a red main.


def test_epic_children_are_expandable_not_reparented():
    # The owner's tree stays intact: children render under their epic via
    # an expander on the board, never by mutating parent_id.
    src = _read(_TASKS)
    assert "childrenByParent" in src or "parent_id" in src, \
        "board must group children under their epic client-side"
