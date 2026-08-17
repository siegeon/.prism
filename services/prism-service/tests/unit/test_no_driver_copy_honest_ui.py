"""The stalled pill never claims "needs you" (owner 2026-08-13).

"stalled · needs you" rendered on every mid-flow task with no live driver --
but with no driver attached the owner has NO affordance to click; the fix is
relaunching the drive, and owner asks exist only at gates (awaiting_gate).
The owner read the board as "I must intervene" over healthy-but-quiet work:
"its very hard to do anything".

The PRISM SPA has NO JS test runner, so UI ACs are pinned by asserting the
ACTUAL TSX source (convention: test_conductor_page_animated_cleanup_ui.py).
Assertions target the RENDERED label strings, never comments -- comments in
these files legitimately discuss the retired wording.
"""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"

SDLC = (WEB / "components" / "conductor" / "SdlcProgress.tsx").read_text(
    encoding="utf-8")
CONDUCTOR = (WEB / "pages" / "ConductorPage.tsx").read_text(encoding="utf-8")
TOKENTURNS = (WEB / "components" / "conductor" / "TokenTurns.tsx").read_text(
    encoding="utf-8")


def test_stalled_label_never_says_needs_you():
    """The exact retired label must not come back on any renderer: the alarm
    words are reserved for states where an owner action exists."""
    for name, src in (("SdlcProgress", SDLC), ("ConductorPage", CONDUCTOR),
                      ("TokenTurns", TOKENTURNS)):
        assert "stalled · needs you" not in src, (
            f"{name} renders 'stalled · needs you' again -- with no driver "
            "attached the owner has nothing to click; superseded by "
            "'no active driver' (owner 2026-08-13)")


def test_stalled_reads_no_active_driver():
    """The honest replacement lives in the ONE shared map."""
    assert 'stalled: { label: "no active driver"' in SDLC
    # SUPERSEDED (task 40c29b83, FR-7): ConductorPage's own private map (this
    # test used to pin a SECOND copy of the literal here) is deleted in favor
    # of importing SdlcProgress.tsx's ACTIVITY_META, so the "no active
    # driver" wording now has exactly ONE source instead of two copies that
    # could drift apart. The invariant survives as "ConductorPage imports
    # the shared map" rather than "ConductorPage repeats the literal".
    assert 'ACTIVITY_META' in CONDUCTOR and '"@/components/conductor/SdlcProgress"' in CONDUCTOR, (
        "ConductorPage must render the stalled label through the shared "
        "ACTIVITY_META import, not a re-declared local copy")


def test_driving_state_renders_the_heartbeat_tool():
    """A running step shows WHAT it is doing (task 9b6800a6): when the server
    threads activity.heartbeat through, the driving pill renders last_tool
    rather than a bare state word."""
    assert 'state === "driving" && hb?.last_tool' in SDLC
    assert 'actState === "driving" && hb?.last_tool' in CONDUCTOR
    # the tile treats a heartbeat-attributed step as live work
    assert 'actState === "driving"' in CONDUCTOR


def test_activity_type_carries_heartbeat():
    """The shared Activity type exposes the heartbeat payload so every
    consumer can render it without re-declaring the shape."""
    assert "heartbeat?:" in SDLC
    assert "last_tool?" in SDLC
