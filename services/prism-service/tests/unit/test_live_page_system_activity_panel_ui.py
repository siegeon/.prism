"""UI contract test for the /live "SYSTEM ACTIVITY" panel (fixer-brief/
activity task, owner: "this is all about visibility / observability of all
processing happening in the system -- lightning fast").

The PRISM SPA has NO JS test runner, so this pins the ACTUAL rendered TSX
source (the same convention as test_conductor_page_animated_cleanup_ui.py):
LivePage.tsx must actually mount SystemActivityPanel (not merely define an
unused component), and SystemActivityPanel.tsx must poll the real
/api/system/activity route, render a live-elapsed "running" section and a
"recent" completed-passes section, and be positioned so it never collides
with the existing gate-decision overlay or reset-layout button.

Docking position SUPERSEDED 2026-09-13 (owner, verbatim, with a screenshot
of the conductor canvas): "i dont want the panel on the top, the playing is
supposed to be IN the graph like in a normal game." The panel moved from a
top-right float to a bottom-right, collapsed-by-default drawer -- see the
docking/collapsed tests below for what replaced the old top-right pin.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_LIVE_PAGE = _SRC / "pages" / "LivePage.tsx"
_PANEL = _SRC / "components" / "live" / "SystemActivityPanel.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_live_page_imports_and_mounts_the_panel() -> None:
    src = _read(_LIVE_PAGE)
    assert 'import SystemActivityPanel from "@/components/live/SystemActivityPanel"' in src
    # Rendered as an actual JSX tag inside the canvas overlay container, not
    # just imported and left unused.
    assert "<SystemActivityPanel" in src
    assert "project={project}" in src[src.index("<SystemActivityPanel"):src.index("<SystemActivityPanel") + 200]


def test_panel_polls_the_real_system_activity_route() -> None:
    src = _read(_PANEL)
    assert "/api/system/activity" in src
    # Task fix/polling: moved off its own bare 1s setTimeout loop onto the
    # shared change-counter gate (counter move / focus / 30s floor, never
    # while hidden) -- still effectively ~1Hz-responsive since wakeups.py
    # signals on exactly the background passes this panel shows.
    assert "usePolledResource" in src
    assert "POLL_MS = 1000" not in src


def test_panel_renders_a_running_section_with_live_elapsed() -> None:
    src = _read(_PANEL)
    assert "snap.running" in src
    # Elapsed for a running pass is recomputed from started_at every tick,
    # never frozen at the value from the last fetch.
    assert "now - e.started_at" in src
    assert "forceTick" in src


def test_panel_renders_a_recent_completed_section() -> None:
    src = _read(_PANEL)
    assert "snap.recent" in src
    assert "recent.slice(0, 20)" in src


def test_panel_is_docked_bottom_right_so_it_never_collides_with_other_overlays() -> None:
    # SUPERSEDED 2026-09-13 (owner, verbatim, with a screenshot of the
    # conductor canvas): "i dont want the panel on the top, the playing is
    # supposed to be IN the graph like in a normal game." A top-right float
    # competed with the board it was meant to describe; it now docks along
    # the bottom, out of the way of the gate-decision panel (inset-3) and
    # the reset-layout button (bottom-left).
    src = _read(_PANEL)
    assert "absolute bottom-3 right-3" in src
    assert "absolute top-3 right-3" not in src


def test_panel_is_collapsed_by_default() -> None:
    # Same owner note: the panel must not impose itself unasked -- it opens
    # to the same detail on a click, but never starts expanded over the
    # graph it now sits beside.
    src = _read(_PANEL)
    assert "useState(true)" in src[:src.index("return (")], (
        "collapsed must default to true; the panel must not auto-expand")


def test_panel_distinguishes_failed_passes_in_the_recent_list() -> None:
    src = _read(_PANEL)
    assert 'e.ok === false' in src
