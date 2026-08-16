"""UI contract tests for /live's watchability slice S1: a mission clock
anchored to a REAL server-recorded drive-start timestamp (task
4e6c4bf3, "Retire the Conductor page into /live's panels", plan_doc S1,
AC-1 + AC-3).

Why this exists (root cause pinned by the task's own stop_if): a mission
clock that anchors to client-side `Date.now()`/`performance.now()` at
first render resets to 0:00 on every page reload and cannot be trusted --
the owner's actual requirement is a clock that "counts a drive from its
real server-stamped claim timestamp... does not reset on reload."

The PRISM SPA has NO JS test runner, so these pin the ACTUAL web source
(TSX/TS) -- same convention as test_live_graph_visual_grammar_ui.py and
test_conductor_page_animated_cleanup_ui.py: assert on real exported
field/function names and rendered strings, never on a comment.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_LIVE = _SRC / "live"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-1: the server field exists on the wire contract and is consumed by
# graphState.ts on bootstrap, never invented client-side.
# ---------------------------------------------------------------------------

def test_types_graphnode_carries_drive_started_at():
    src = _read(_LIVE / "types.ts")
    assert "drive_started_at" in src, (
        "GraphNode must carry the server-computed drive_started_at field "
        "(epoch seconds of the task's earliest agent_runs row) so the "
        "mission clock has a real per-task anchor to render from")


def test_graph_state_stores_drive_started_at_per_node():
    src = _read(_LIVE / "graphState.ts")
    assert "driveStartedAt" in src, (
        "LiveNode must carry a driveStartedAt field, mirroring the "
        "existing gate_waiting_s -> gatePendingSince backdating pattern "
        "(never a bare Date.now()/performance.now() invented at render)")


def test_graph_state_computes_server_clock_skew_from_generated_at():
    src = _read(_LIVE / "graphState.ts")
    assert "generated_at" in src, (
        "GraphState must read the boot snapshot's own generated_at to "
        "derive a client/server clock skew once, so the mission clock "
        "counts from the SERVER's clock, not the browser's")
    assert "SkewMs" in src or "skewMs" in src or "ClockSkew" in src, (
        "a named clock-skew field/computation must exist -- deriving the "
        "mission elapsed time straight off a raw Date.now() minus a "
        "server epoch, unadjusted for skew, is exactly the client-`now` "
        "anti-pattern the task's stop_if forbids")


# ---------------------------------------------------------------------------
# AC-1 + AC-3: the rendered clock reads a per-node server field, never a
# page-level/shared variable (would bleed across tasks, mx-9f2018).
# ---------------------------------------------------------------------------

def test_mission_clock_is_rendered_somewhere_in_live():
    hud_src = _read(_LIVE / "hud.ts")
    draw_src = _read(_LIVE / "draw.ts")
    combined = hud_src + draw_src
    assert "MISSION" in combined, (
        "a MISSION clock label must actually be drawn on /live -- no "
        "server field is worth anything without a renderer in the same "
        "slice (the 0784729f backend-only misfire)")
    assert "driveStartedAt" in combined, (
        "the rendered clock must read the per-node driveStartedAt field, "
        "not a page-level 'now' variable shared across every card on "
        "screen (cross-task gauge bleed, mx-9f2018)")


def test_mission_clock_never_anchors_to_a_bare_now_at_first_render():
    hud_src = _read(_LIVE / "hud.ts")
    draw_src = _read(_LIVE / "draw.ts")
    combined = hud_src + draw_src
    # The forbidden shape this guards against: seeding the mission clock's
    # start moment from the browser's own clock the instant the module
    # first runs (would reset to 0:00 on every reload).
    assert "missionStart = Date.now()" not in combined
    assert "missionStart = performance.now()" not in combined
