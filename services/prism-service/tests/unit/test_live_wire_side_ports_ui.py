"""Pinned UI contract for /live wire PORTS (task b9ce0450): every wire
docks at one draggable port dot on its card's perimeter, auto-placed by
default and re-dockable to any side/offset by the owner, drawn under the
cards, hit-testable via the same geometry the renderer uses, routed
orthogonally around card obstacles, and persisted per project.

Same no-JS-runner convention as test_live_graph_visual_grammar_ui.py:
pins the ACTUAL ts/tsx source via real exported names and the enclosing
function BODY (comment-stripped, brace-matched) -- never a bare
substring match a doc comment could satisfy, and never a fixed
character window that a comment above the code could push the real
guard out of.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_LIVE = _SRC / "live"
_LIVE_PAGE = _SRC / "pages" / "LivePage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Removes /* */ blocks and // line comments so a doc comment
    mentioning a symbol can never satisfy an assertion about the
    symbol's real code (a comment has satisfied this kind of assertion
    before -- see wires.ts/cards.ts history)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    lines = []
    for line in src.split("\n"):
        idx = line.find("//")
        lines.append(line if idx == -1 else line[:idx])
    return "\n".join(lines)


def _function_body(src: str, signature: str) -> str:
    """Finds `signature` (e.g. "export function portPoint(") in the
    COMMENT-STRIPPED source and returns everything between its opening
    `{` and matching closing `}`, counted by brace depth -- never a
    fixed character window, which a comment above a function has been
    shown to push the real guard out of."""
    stripped = _strip_comments(src)
    idx = stripped.find(signature)
    assert idx != -1, f"{signature!r} not found in source"
    brace_start = stripped.find("{", idx)
    assert brace_start != -1, f"no body opened after {signature!r}"
    depth = 0
    i = brace_start
    while i < len(stripped):
        ch = stripped[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped[brace_start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces after {signature!r}")


# ---------------------------------------------------------------------------
# FR-1 / AC-1 / AC-2: wires.ts is the SINGLE geometry source of truth --
# PortSide (4 sides), WirePort{side,t}, portPoint, portFromWorld, autoPort,
# wireKey, laneFor, drawPort all live there, not a second copy anywhere.
# ---------------------------------------------------------------------------

def test_wires_exports_the_port_geometry_api():
    src = _strip_comments(_read(_LIVE / "wires.ts"))
    assert "export type PortSide" in src, "wires.ts must export a PortSide union"
    assert '"left"' in src and '"right"' in src and '"top"' in src and '"bottom"' in src, (
        "PortSide must admit all four sides, not just left/right")
    assert "export type WirePort" in src, "wires.ts must export a WirePort {side, t} descriptor"
    for fn in ("portPoint(", "portFromWorld(", "autoPort(", "wireKey(", "laneFor(", "drawPort("):
        assert f"export function {fn}" in src, f"wires.ts must export {fn} -- the port geometry API"


def test_port_point_branches_on_all_four_sides_with_a_continuous_offset():
    src = _read(_LIVE / "wires.ts")
    body = _function_body(src, "export function portPoint(")
    for side in ('"left"', '"right"', '"top"', '"bottom"'):
        assert side in body, f"portPoint must have a branch for {side} (AC-2: any of the 4 sides)"
    assert re.search(r"\bport\.t\b|\bt\b", body), (
        "portPoint must use a continuous offset t along the side, not a fixed center")


def test_port_point_is_defined_exactly_once_across_live_ts_no_second_copy():
    # AC-8 / mx-0a0bf4: the hit-test must MIRROR the draw, never re-derive a
    # second copy of the side/offset math. Count DEFINITIONS (not calls) of
    # portPoint across every live/*.ts file.
    definitions = 0
    for p in _LIVE.glob("*.ts"):
        src = _strip_comments(_read(p))
        definitions += len(re.findall(r"function\s+portPoint\s*\(", src))
    assert definitions == 1, (
        f"portPoint must be defined exactly once across live/*.ts, found {definitions}")


# ---------------------------------------------------------------------------
# FR-2 / FR-3 / FR-4 / AC-3 / AC-4: default placement faces the peer with
# per-wire lanes, and routing stubs perpendicular out of the port then
# treats card rectangles as obstacles.
# ---------------------------------------------------------------------------

def test_auto_port_derives_its_offset_from_lane_not_a_constant():
    src = _read(_LIVE / "wires.ts")
    body = _function_body(src, "export function autoPort(")
    assert "lane" in body, (
        "autoPort must derive its per-wire offset from the lane argument "
        "(AC-3: two wires on the same card must never resolve to the same point)")


def test_lane_for_hashes_the_wire_key():
    src = _read(_LIVE / "wires.ts")
    body = _function_body(src, "export function laneFor(")
    assert re.search(r"\bkey\b", body), "laneFor must derive a lane from the wire key argument"


def test_route_orthogonal_gains_optional_route_opts_with_ports_and_obstacles():
    src = _read(_LIVE / "wires.ts")
    stripped = _strip_comments(src)
    assert "export type RouteOpts" in stripped, "wires.ts must export a RouteOpts type"
    sig_idx = stripped.find("export function routeOrthogonal(")
    assert sig_idx != -1, "routeOrthogonal must still exist under its own name"
    brace_idx = stripped.find("{", sig_idx)
    signature_text = stripped[sig_idx:brace_idx]
    assert "RouteOpts" in signature_text, (
        "routeOrthogonal must gain an optional third RouteOpts argument")
    body = _function_body(src, "export function routeOrthogonal(")
    assert "obstacles" in body, "routeOrthogonal must consult obstacle rectangles (AC-4)"
    assert "fromPort" in body or "toPort" in body, (
        "routeOrthogonal must use the supplied ports to emit a perpendicular stub (AC-4)")


# ---------------------------------------------------------------------------
# FR-5 / AC-1 / AC-5: ONE router entry point -- draw.ts must go through
# GraphState.wireEndpointsFor and draw a port dot, never call
# routeOrthogonal itself (the old duplicate call site is DELETED).
# ---------------------------------------------------------------------------

def test_draw_ts_has_a_single_router_entry_point_no_duplicate_route_call():
    src = _strip_comments(_read(_LIVE / "draw.ts"))
    assert "state.wireEndpointsFor(" in src, (
        "draw.ts must resolve wires through GraphState.wireEndpointsFor, "
        "the single router entry point (AC-5)")
    assert "routeOrthogonal(" not in src, (
        "draw.ts must NOT call routeOrthogonal directly any more -- that "
        "duplicate call site is what let the drawn wire and the packet "
        "path diverge (AC-5)")


def test_draw_ts_draws_a_port_dot_per_endpoint():
    src = _strip_comments(_read(_LIVE / "draw.ts"))
    assert "drawPort(" in src, "draw.ts's wire loop must draw a port dot per endpoint (AC-1)"


# ---------------------------------------------------------------------------
# FR-1 / FR-8 / AC-6 / AC-8 / AC-9: graphState.ts mirrors the position-
# override machinery field-by-field for ports, portAtWorld calls the SAME
# portPoint the renderer uses, and clearAllOverrides resets BOTH maps.
# ---------------------------------------------------------------------------

def test_graph_state_declares_port_override_machinery():
    src = _strip_comments(_read(_LIVE / "graphState.ts"))
    for member in (
        "portOverrides", "setPortOverride", "hydratePortOverrides",
        "serializePortOverrides", "portAtWorld", "draggingPortId",
    ):
        assert member in src, f"GraphState must declare {member} (mirrors the position-override machinery)"


def test_clear_all_overrides_clears_both_position_and_port_maps():
    src = _read(_LIVE / "graphState.ts")
    body = _function_body(src, "clearAllOverrides(): void")
    assert "overrides" in body and "portOverrides" in body, (
        "clearAllOverrides must clear BOTH maps so 'reset layout' resets everything (AC-9)")


def test_port_at_world_calls_portPoint_mirroring_the_draw():
    src = _read(_LIVE / "graphState.ts")
    body = _function_body(src, "portAtWorld(")
    assert "portPoint(" in body, (
        "portAtWorld must call the SAME portPoint geometry the renderer "
        "uses (AC-8, mx-0a0bf4), never re-derive its own copy")


# ---------------------------------------------------------------------------
# FR-6 / FR-7 / AC-6 / AC-7: LivePage gains a "port" DragMode whose hit-test
# sits strictly between the action-strip check and nodeAtWorld -- the exact
# ordering that disambiguates the new gesture from card-drag and pan.
# ---------------------------------------------------------------------------

def test_live_page_drag_mode_gains_port():
    src = _strip_comments(_read(_LIVE_PAGE))
    assert re.search(r'type DragMode\s*=\s*"none"\s*\|\s*"pan"\s*\|\s*"node"\s*\|\s*"port"', src), (
        'DragMode must gain a fourth "port" value')


def test_live_page_port_hit_test_is_ordered_between_action_strip_and_node():
    src = _strip_comments(_read(_LIVE_PAGE))
    down_start = src.find("const onPointerDown")
    assert down_start != -1, "onPointerDown must exist"
    down_end = src.find("const onPointerMove")
    assert down_end != -1 and down_end > down_start
    down_body = src[down_start:down_end]

    strip_idx = down_body.find("actionStripHitTest(")
    port_idx = down_body.find("portAtWorld(")
    node_idx = down_body.find("nodeAtWorld(")
    assert strip_idx != -1, "action-strip check must still run first in onPointerDown"
    assert port_idx != -1, "onPointerDown must hit-test a port BEFORE a card body (AC-7)"
    assert node_idx != -1, "onPointerDown must still hit-test the card body"
    assert strip_idx < port_idx < node_idx, (
        "ordering must be action-strip, then portAtWorld, then nodeAtWorld, "
        "then pan (this exact order is the stop_if's disambiguation answer)")


# ---------------------------------------------------------------------------
# FR-8 / AC-9: manual port placements persist per project, following the
# EXACT prism.live.*.<project> convention the position overrides already
# use -- hydrated after bootstrap, written on release, cleared by reset.
# ---------------------------------------------------------------------------

def test_live_page_ports_key_follows_the_prism_live_convention():
    src = _strip_comments(_read(_LIVE_PAGE))
    assert re.search(r"`prism\.live\.ports\.\$\{project\}`", src), (
        "the port-overrides localStorage key must follow the "
        "prism.live.ports.<project> convention (AC-9)")


def test_live_page_hydrates_persists_and_resets_port_overrides():
    src = _strip_comments(_read(_LIVE_PAGE))
    assert "hydratePortOverrides(" in src, "port overrides must rehydrate after bootstrap"
    assert "serializePortOverrides(" in src, "port overrides must be persisted on release"
    reset_start = src.find("const handleResetLayout")
    next_const = src.find("const onPointerDown")
    assert reset_start != -1 and next_const != -1 and reset_start < next_const, (
        "handleResetLayout must exist before onPointerDown in source order")
    reset_body = src[reset_start:next_const]
    assert "portsKey" in reset_body or "prism.live.ports." in reset_body, (
        "'reset layout' must also drop the persisted port overrides (AC-9)")


# ---------------------------------------------------------------------------
# task 763168f8: the drag handle must be SEEN. Idle wires draw at 35% alpha
# and drawPort inherited that exact color, so the shipped movable-port
# feature was invisible on a quiet board (owner: "i dont even see my
# movable lines").
# ---------------------------------------------------------------------------

def test_port_dots_visible_when_idle():
    """AC-1: drawPort selects its own fill by `live` -- the idle arm is a
    full-opacity literal, never the wire's 0.35-alpha stroke -- and the
    dot is big enough to read at default zoom."""
    src = _strip_comments(_read(_LIVE / "wires.ts"))
    body = _function_body(src, "export function drawPort")
    assert "live ?" in body or "live\n" in body, (
        "drawPort must branch its fill on `live` instead of trusting the "
        "caller's wire color for the idle case")
    assert "0.35" not in body, "the idle handle must not reuse the dim wire alpha"
    assert re.search(r'#[0-9a-fA-F]{6}|PORT_HANDLE', body), (
        "the idle arm needs a full-opacity handle color literal/constant")
    m = re.search(r"PORT_DOT_R\s*=\s*(\d+)", src)
    assert m and int(m.group(1)) >= 4, "PORT_DOT_R must be >= 4 to read at default zoom"


def test_port_hover_shows_grab_cursor():
    """AC-2: pointermove outside an active drag consults the REAL
    portAtWorld hit-test and the canvas className gains a cursor-grab
    branch keyed on it."""
    src = _strip_comments(_read(_LIVE_PAGE))
    assert "portAtWorld" in src.split("const onPointerMove", 1)[1].split("const onPointerUp", 1)[0], (
        "onPointerMove must hit-test portAtWorld when no drag is active")
    cls = src[src.index("className="):]
    cls = cls[: cls.index(">")]
    assert "cursor-grab" in cls and "cursor-grabbing" in cls and "cursor-pointer" in cls, (
        "the canvas className must offer grab (hover), grabbing (drag) and pointer")


def test_idle_wire_stays_dim():
    """AC-3 anti-misfire: fixing the DOT must not brighten the idle WIRE."""
    src = _strip_comments(_read(_LIVE / "wires.ts"))
    body = _function_body(src, "export function wireColor")
    assert "0.35" in body, (
        "wireColor's non-flowing arm must keep the dim 0.35 stroke -- "
        "brightening the wire is the named misfire, not the fix")
