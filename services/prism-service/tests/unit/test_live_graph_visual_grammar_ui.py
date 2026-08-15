"""UI contract tests for the /live graph rebuild (gauntlet piece 1, "the
living graph") — every running task is a card-node on a deterministic
circuit-board canvas, matching E:\\gamify-lab\\DESIGN_DIRECTIVE.md +
reference/VISUAL_GRAMMAR.md.

The PRISM SPA has NO JS test runner, so these pin the ACTUAL web source
(TSX/TS) — same convention as test_conductor_page_animated_cleanup_ui.py:
assert on real exported function names / structure / rendered strings,
never on comments.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_LIVE = _SRC / "live"
_LIVE_PAGE = _SRC / "pages" / "LivePage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Module seams exist (layout/cards/wires/packets/hud/idle/graphState + one
# render-loop owner), so parallel lanes can each own a file.
# ---------------------------------------------------------------------------

def test_live_modules_exist_with_clean_seams():
    for name in (
        "layout.ts", "cards.ts", "wires.ts", "packets.ts",
        "hud.ts", "idle.ts", "graphState.ts", "draw.ts", "palette.ts",
    ):
        assert (_LIVE / name).is_file(), f"src/live/{name} must exist"


def test_layout_is_deterministic_not_physics():
    src = _read(_LIVE / "layout.ts")
    assert "class LayoutEngine" in src, "layout.ts must export a LayoutEngine"
    assert "placeTask" in src and "placeSubtask" in src and "placeSession" in src, (
        "LayoutEngine must place tasks, subtasks and sessions as distinct slots")
    assert "d3-force" not in src, "layout must be deterministic, not physics-based"


def test_graph_state_no_longer_uses_d3_force():
    src = _read(_LIVE / "graphState.ts")
    assert "d3-force" not in src, (
        "graphState.ts dropped the force-directed blob simulation for the "
        "directive's deterministic auto-layout")
    assert "class GraphState" in src
    assert "LayoutEngine" in src, "GraphState must delegate positions to layout.ts"


# ---------------------------------------------------------------------------
# LivePage renders the canvas, subscribes /sse/work, and drives pan/zoom.
# ---------------------------------------------------------------------------

def test_live_page_renders_canvas_and_subscribes_sse_work():
    src = _read(_LIVE_PAGE)
    assert "<canvas" in src, "LivePage must render a canvas element"
    assert "/sse/work?project=" in src, "LivePage must subscribe to /sse/work"
    assert "/api/work/graph?project=" in src, "LivePage must boot from /api/work/graph"


def test_live_page_supports_pan_and_zoom():
    src = _read(_LIVE_PAGE)
    assert "onWheel" in src, "wheel zoom must be wired on the canvas"
    assert "onPointerDown" in src and "onPointerMove" in src and "onPointerUp" in src, (
        "drag-to-pan must be wired via pointer events on the canvas")


def test_live_page_click_navigates_via_node_href():
    src = _read(_LIVE_PAGE)
    assert "navigate(node.href)" in src, (
        "a click on an already-selected card must navigate to node.href")


def test_live_page_canvas_is_devicepixelratio_crisp():
    src = _read(_LIVE_PAGE)
    assert "devicePixelRatio" in src, (
        "the canvas backing store must be scaled by devicePixelRatio for crisp rendering")


# ---------------------------------------------------------------------------
# cards.ts draws the card anatomy the directive specifies: title, glyph,
# Tokens/Step/Spend stat rows with connector dots, capacity bar, gate row,
# selection outline.
# ---------------------------------------------------------------------------

def test_cards_draws_title_bar_and_glyph():
    src = _read(_LIVE / "cards.ts")
    assert "export function drawCard(" in src
    assert "glyphFor(n.kind)" in src, "title bar must render the kind's leading glyph"
    assert "TITLE_H" in src, "a distinct title-bar band must be drawn"


def test_cards_draws_tokens_step_spend_stat_rows():
    src = _read(_LIVE / "cards.ts")
    assert '"Tokens"' in src, "Tokens row (teal) must be drawn"
    assert '"Step"' in src, "Step row (orange) must be drawn"
    assert '"Spend"' in src, "Spend row (green) must be drawn"
    assert "drawCapacityBar(" in src, "Step row must carry a filling capacity bar"


def test_cards_draws_connector_dots_with_dead_ring_fallback():
    src = _read(_LIVE / "cards.ts")
    assert "drawConnectorDot(" in src
    assert "PALETTE.red" in src, (
        "a dead/absent signal must fall back to the reserved red hollow ring")


def test_cards_draws_gate_row_only_when_pending():
    src = _read(_LIVE / "cards.ts")
    assert "m.gatePending" in src, "gate row must be conditional on a pending gate"
    assert "PALETTE.magenta" in src, "gate row must use the locked magenta meaning"


def test_cards_draws_selection_outline_distinct_from_state_colors():
    src = _read(_LIVE / "cards.ts")
    assert "n.selected" in src
    assert "PALETTE.selection" in src, "selection outline must use its own dedicated color"


def test_cards_ghosts_values_on_change_not_just_a_static_digit():
    src = _read(_LIVE / "cards.ts")
    assert "drawGhostable(" in src, (
        "a changed stat must render a double-exposed ghost, not just a static digit swap")
    assert "tokensGhostUntil" in src and "stepGhostUntil" in src


# ---------------------------------------------------------------------------
# wires.ts routes orthogonally (90-degree elbows), never diagonal, and
# in-transit packets ride the same polyline.
# ---------------------------------------------------------------------------

def test_wires_routes_orthogonally():
    src = _read(_LIVE / "wires.ts")
    assert "export function routeOrthogonal(" in src
    # An elbow connector emits a 4-point polyline (start, two mid corners,
    # end) built from horizontal/vertical moves only — never a bare
    # 2-point diagonal segment between arbitrary card centers.
    assert "midX" in src or "midY" in src, (
        "routeOrthogonal must jog through a mid corner, not draw a diagonal")


def test_wires_colors_are_locked_by_flow_type():
    src = _read(_LIVE / "wires.ts")
    assert "PALETTE.teal" in src, "token-flow wires must use the locked teal meaning"
    assert 'kind === "token"' in src, "wire color must branch on WHAT is flowing"


def test_packets_ride_the_wire_polyline():
    src = _read(_LIVE / "packets.ts")
    assert "pointAtFraction(" in src, (
        "packets must be placed via the wire's own polyline math, not a straight lerp")
    assert "export function spawnPacket(" in src
    assert "export function stepPackets(" in src


# ---------------------------------------------------------------------------
# hud.ts is fixed/screen-space and carries a sparkline; idle.ts has no
# full-screen dead state once booted.
# ---------------------------------------------------------------------------

def test_hud_reports_aggregate_throughput_and_a_sparkline():
    src = _read(_LIVE / "hud.ts")
    assert "export function drawHud(" in src
    assert "tokSHistory" in src, "HUD must draw the aggregate tok/s trend, not just a number"


def test_idle_has_no_fullscreen_dead_state_once_booted():
    src = _read(_LIVE / "idle.ts")
    assert "export function drawLoading(" in src, "pre-content boot uses a bare-grid loading state"
    assert "queue is quiet" in src, (
        "a quiet graph reads as a small HUD-area line, never a full-screen empty state")


# ---------------------------------------------------------------------------
# draw.ts is the single render-loop owner: it composes grid, wires,
# packets, cards and HUD, and never animates on a bare timer (motion is
# WorkEvent-sourced only, wired through GraphState.applyEvent/step).
# ---------------------------------------------------------------------------

def test_draw_composes_every_sub_renderer():
    src = _read(_LIVE / "draw.ts")
    for fn in ("drawWire(", "drawPackets(", "drawCard(", "drawHud("):
        assert fn in src, f"draw.ts must call {fn} — it is the render loop owner"


def test_version_bumped_for_this_change():
    # Pinned as a FLOOR, never equality on the live gamify.N marker: an
    # exact-literal pin rots on the very next patch bump and hands its red
    # to whichever lane bumps next (same lesson as AC-9 in
    # test_second_machine_can_reach_prism_ui.py). This slice's own bump
    # (7.10.54+gamify.2) is the floor "piece 1" established; anything at
    # or past it satisfies "the gamify version marker was bumped".
    import re

    ver_src = _read(_HERE.parent.parent.parent / "prism_service" / "__version__.py")
    m = re.search(r'PRISM_VERSION = "7\.10\.54\+gamify\.(\d+)"', ver_src)
    assert m, (
        "PRISM_VERSION must stay a readable 7.10.54+gamify.N marker; "
        f"got a version line that doesn't match: {ver_src.splitlines()[:1]!r}")
    assert int(m.group(1)) >= 2, (
        "gauntlet piece 1 must bump the gamify version marker to at least "
        f".2; got .{m.group(1)}")
