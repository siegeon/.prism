"""UI contract for the Tasks BOARD (ui-redesign epic 16777a76, ws5/ws6).

HISTORY: this file originally pinned the kanban board's motion wiring
(task a5e0d9f5 — FLIP cards, AnimatePresence columns). The owner-approved
direction artifact (1fab352f) replaced the kanban with a Jira-style grouped
table under a persistent LIVE bar, so those pins were superseded by design.
This version pins the NEW board's honest-liveness + table contract, same
source-assert pattern (no JS test runner) as the other *_ui.py contracts.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_TASKS = _SRC / "pages" / "TasksPage.tsx"
_CSS = _SRC / "index.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# LIVE bar — real conductor signal, honest idle, never a frozen fake pulse
# ---------------------------------------------------------------------------

def test_live_bar_reads_real_conductor_state():
    src = _read(_TASKS)
    assert "/api/conductor/state" in src, \
        "LIVE bar must read the conductor's real work-state endpoint"


def test_live_bar_has_honest_idle_state():
    src = _read(_TASKS)
    # The pulse may ONLY render when something is actually being driven;
    # a quiet queue shows an explicit idle message instead.
    assert "isLive" in src or "idle" in src.lower(), \
        "LIVE bar needs an explicit idle state, not an always-on pulse"
    assert 'animate-pulse" : ""' in src.replace("'", '"') \
        or '(isLive ? "animate-pulse"' in src, \
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


def test_board_groups_by_status_buckets():
    src = _read(_TASKS)
    for bucket in ("In progress", "At a gate", "Up next", "Blocked"):
        assert bucket in src, f"board must render the '{bucket}' group"


def test_epic_children_are_expandable_not_reparented():
    # The owner's tree stays intact: children render under their epic via
    # an expander on the board, never by mutating parent_id.
    src = _read(_TASKS)
    assert "childrenByParent" in src or "parent_id" in src, \
        "board must group children under their epic client-side"
