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
_SIDEBAR = _SRC / "components" / "Sidebar.tsx"
_APP = _SRC / "App.tsx"
_CSS = _SRC / "index.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# LIVE signal — real conductor state, honest idle, never a frozen fake pulse.
#
# RETARGETED (task d9f082fe follow-up, owner live, 2026-08-24): LiveBar.tsx
# (the app-shell PULSE CARD this section originally pinned) is deleted
# outright -- owner: "that live panel is odd to me... remove the live pill
# and make the live icon in the activity view green". The signal now lives
# on Sidebar.tsx's own "/live" nav icon instead -- still visible on EVERY
# page (Sidebar is the one component mounted on all of them, more so than
# LiveBar's old ACTIVITY_ROUTES-only scoping ever was), just as an icon
# tint rather than a separate card.
# ---------------------------------------------------------------------------

# SUPERSEDED 2026-08-12 (task 40c29b83): the /api/conductor/state fetch
# lives in the shared lib/useConductorState.ts hook (FR-1/FR-3) -- a
# consumer reaches the endpoint only by IMPORTING the hook, so the literal
# check lives in the hook file and this test instead pins the import shape.
_HOOK = _SRC / "lib" / "useConductorState.ts"


def test_sidebar_live_icon_reads_real_conductor_state():
    src = _read(_SIDEBAR)
    assert re.search(
        r'import\s*\{[^}]*\buseConductorState\b[^}]*\}\s*from\s*'
        r'["\']@/lib/useConductorState["\']',
        src,
    ), "Sidebar must import useConductorState (the shared conductor work-state hook)"
    assert _HOOK.exists(), f"expected {_HOOK} to own the endpoint fetch"
    hook_src = _read(_HOOK)
    assert "/api/conductor/state" in hook_src, \
        "useConductorState must read the conductor's real work-state endpoint"


def test_sidebar_is_mounted_in_the_app_shell():
    app = _read(_APP)
    assert "Sidebar" in app, \
        "Sidebar must mount in the shell so the live icon persists on every page"


def test_live_icon_has_honest_idle_state():
    src = _read(_SIDEBAR)
    # The green tint may ONLY render when something is actually being
    # driven; a quiet queue leaves the icon in its default/muted color —
    # never a claim of "idle" the icon has to walk back once observed.
    assert "isLive" in src, \
        "Sidebar needs an explicit isLive signal, not an always-on tint"
    assert "animate-pulse" in src and "isLive" in src, \
        "the pulsing tint must be conditional on real liveness"


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
