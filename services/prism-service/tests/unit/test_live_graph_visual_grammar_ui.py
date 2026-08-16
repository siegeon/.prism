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
    # Round 3 item 2 SUPERSEDES the plain glyphFor(n.kind) call: a
    # session/agent card's glyph now also varies by role (dev/qa/sm), so
    # the title bar passes n.role through too.
    assert "glyphFor(n.kind, n.role)" in src, "title bar must render the kind+role glyph"
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


# ---------------------------------------------------------------------------
# Round 2 (gauntlet pieces 2 + 5): structural edges carry motion too, task/
# subtask cards get a real throughput buffer bar, and the HUD reads as
# instrumentation even at a squint. Pinned against the blind critics'
# verdicts in E:\gamify-lab\verdicts\round1\piece2_edge_motion.md and
# piece5_readouts.md.
# ---------------------------------------------------------------------------

def test_packets_travel_at_a_constant_speed_not_scaled_by_tok_s():
    src = _read(_LIVE / "packets.ts")
    assert "export function spawnPacket(edgeKey: string, pts: Point[]): Packet" in src, (
        "spawnPacket must no longer take tokS -- speed is constant per the "
        "build directive, only spawn FREQUENCY scales with throughput")
    assert "polylineLength" in src, (
        "packet fractional speed must be derived from the wire's real pixel "
        "length so travel is a CONSTANT real-world px/s, not a fixed fraction")
    assert "speedForTokS" not in src, "the old tok_s-scaled speed model must be gone"


def test_structural_wire_carries_propagated_flow_upward():
    src = _read(_LIVE / "graphState.ts")
    assert "parentTaskId" in src and "parent_of" in src, (
        "a subtask's tokens.turn must locate its parent_of structural edge")
    assert "reverse()" in src, (
        "the propagated marker must travel child->parent, the reverse of "
        "the parent_of edge's own source->target direction")
    assert "isEdgeFlowing(" in src, (
        "GraphState must expose whether a specific edge has flowed recently, "
        "for the structural wire's teal tint")


def test_wires_tint_structural_edges_on_recent_flow_not_always_grey():
    src = _read(_LIVE / "wires.ts")
    assert "wireColor(kind: WireKind, flowing: boolean)" in src, (
        "wireColor must take a flowing signal so a structural wire can tint "
        "teal while flow has propagated up it (round1 gap: structural edges "
        "never showed color, a marker, or motion)")


def test_packet_spawns_are_cooldown_gated_per_edge_sparse():
    src = _read(_LIVE / "graphState.ts")
    assert "SPAWN_COOLDOWN_MS" in src, (
        "a wire must carry at most one marker per cooldown window -- "
        "grammar's SPARSE density, not a packet train")
    assert "maybeSpawnPacket" in src or "edgeLastSpawnAt" in src


def test_task_card_gets_a_throughput_buffer_bar_that_drains():
    cards_src = _read(_LIVE / "cards.ts")
    state_src = _read(_LIVE / "graphState.ts")
    assert "bufferFrac" in cards_src and "bufferFrac" in state_src, (
        "a task/subtask card's buffer bar must be a real per-node field, "
        "not a static icon (round1 gap: 'no node ever shows load as a "
        "filling bar')")
    # Round 3 item 4 SUPERSEDES round 2's fixed-span fill/drain model
    # (BUFFER_DRAIN_PER_MS + bufferFillFor against a constant span) with a
    # per-card ROLLING PEAK gauge that drains over a real ~4s window --
    # see graphState.ts's bufferLevelFrac doc for why the fixed span
    # never actually produced "siblings at visibly different fills".
    assert "BUFFER_DRAIN_WINDOW_MS" in state_src, (
        "the buffer bar must drain to 0 over a real elapsed-time window "
        "since the last tokens.turn, not just ratchet up and sit at its "
        "high-water mark")
    assert "bufferPeak" in state_src, (
        "the buffer bar's fill must be relative to a per-card ROLLING "
        "MAX (current/peak), not a fixed constant span shared by every "
        "card regardless of its own typical throughput")
    assert "function bufferLevelFrac(" in state_src, (
        "the buffer bar must fill by an amount driven by the REAL tok_s "
        "on the tokens.turn event, never a fixed timer tick")


def test_hud_rebuilt_with_meter_bars_and_bigger_hero_number():
    src = _read(_LIVE / "hud.ts")
    assert "drawGhostable" in src, (
        "HUD numbers must ghost/tween on change, reusing cards.ts's own "
        "technique per the build directive")
    assert "drawMeter(" in src and "drawSegments(" in src, (
        "every HUD stat row must carry its own meter bar or segmented-block "
        "gauge, not just a bare number (round1 gap: squinted to 25% the "
        "dashboard read as 'mostly empty dark canvas')")
    assert "PANEL_W = 320" in src or "PANEL_W" in src


def test_hud_no_longer_carries_dead_unused_rows():
    src = _read(_LIVE / "hud.ts")
    # round1's own critique of clip A: "two of its four rows never change
    # at all in 78 seconds, so half the HUD is dead weight" -- the rebuilt
    # HUD drops the static "root tasks" counter rather than keep dead rows.
    assert "root tasks" not in src


def test_graph_state_self_heals_nodes_created_after_boot():
    src = _read(_LIVE / "graphState.ts")
    assert "ensureTaskNode" in src, (
        "a task/subtask referenced by an event that arrives after the page's "
        "boot snapshot must get a placeholder card, not be silently dropped "
        "forever (piece-4 cross-check: HUD ticked while cards read 0)")
    # every one of the four WorkEvent handlers must route through the
    # self-healing lookup rather than a bare byId.get(...) early return.
    assert src.count("ensureTaskNode(event.task_id, now)") >= 4


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
    assert int(m.group(1)) >= 6, (
        "round 3 must bump the gamify version marker to at least "
        f".6; got .{m.group(1)}")


# ---------------------------------------------------------------------------
# Round 2 (gauntlet pieces 3 + 4): a real per-card state system with
# color/form driving legibility ("node state while working"), and an
# honest idle state that actually reads quiet-but-healthy rather than
# hung. Pinned against E:\gamify-lab\verdicts\round1\piece3_node_states.md
# and piece4_idle_state.md.
# ---------------------------------------------------------------------------

def test_graph_state_derives_five_named_card_states():
    src = _read(_LIVE / "graphState.ts")
    assert "export function deriveCardState(" in src
    for state in ('"working"', '"waiting_gate"', '"stalled"', '"young"', '"done"'):
        assert state in src, f"deriveCardState must be able to return {state}"


def test_red_ring_color_is_reserved_for_stalled_only_never_hardcoded():
    palette_src = _read(_LIVE / "palette.ts")
    cards_src = _read(_LIVE / "cards.ts")
    # The actual guard, pinned as the real conditional (not a bare
    # substring match): deadRingColorFor only returns PALETTE.red on the
    # "stalled" branch. Round1's exact failure was a red dot rendered
    # UNCONDITIONALLY on every Spend/Step row regardless of state.
    assert 'if (state === "stalled") return PALETTE.red;' in palette_src
    # Every connector dot's dead-fallback must route through this one
    # function -- never a hardcoded PALETTE.red re-introduced on a row.
    assert "deadRingColorFor(state)" in cards_src
    assert cards_src.count("drawConnectorDot(ctx, dotX, rowY,") >= 3, (
        "Tokens/Step/Spend rows must all pass through the same "
        "state-derived dead-ring color, not their own hardcoded fallback")


def test_waiting_gate_state_gets_magenta_stripe_distinct_from_stalled_red():
    src = _read(_LIVE / "cards.ts")
    assert 'state === "waiting_gate"' in src and 'state === "stalled"' in src
    assert "EDGE_STRIPE_W" in src and "PALETTE.magenta" in src, (
        "a gate-pending card must render a magenta left-edge stripe")
    assert "rgba(8,9,13,0.4)" in src, (
        "a stalled card must desaturate its body, distinct from a "
        "gate-pending card's magenta stripe")


def test_done_card_settles_then_compacts_to_a_witnessed_chip():
    src = _read(_LIVE / "cards.ts")
    assert "drawDoneChip(" in src
    assert "COMPACT_AFTER_MS" in src
    assert "settleUntil" in src, (
        "a completed card must get a brief settle flash before it "
        "compacts -- build directive: 'the completion must be witnessed'")


def test_toasts_module_exists_and_graphstate_spawns_done_only():
    toasts_src = _read(_LIVE / "toasts.ts")
    assert "export function spawnToast(" in toasts_src
    assert "export function drawToasts(" in toasts_src
    assert "export function pruneToasts(" in toasts_src

    state_src = _read(_LIVE / "graphState.ts")
    assert 'spawnToast(this.toasts, "done"' in state_src
    # Round 3 item 7 SUPERSEDES round 2's second "gate" toast kind: a
    # gate wait is an ONGOING state (can last minutes), not a momentary
    # notification, so it no longer spawns a one-shot toast at all -- it
    # gets gatepanel.ts's persistent docked panel instead (see the
    # dedicated test below). Toasts stay reserved for what's actually
    # momentary: a task settling to done.
    assert 'spawnToast(this.toasts, "gate"' not in state_src, (
        "a gate wait must no longer fire a momentary toast -- it's a "
        "persistent panel now (round 3 item 7)")

    draw_src = _read(_LIVE / "draw.ts")
    assert "drawToasts(" in draw_src


def test_idle_quiet_line_carries_a_ticking_elapsed_counter():
    src = _read(_LIVE / "idle.ts")
    assert "quietForS" in src, (
        "the quiet line must show elapsed time since last activity -- a "
        "static 'queue is quiet' string with no motion at all is exactly "
        "the round1 gap: 'our clip never actually stages a quiet moment'")
    assert "lastEventAt" in src, (
        "quiet detection must key off GraphState's single real-event "
        "clock, not a per-node tok_s/pulseUntil re-derivation")


def test_graph_state_decays_stale_tok_s_for_honest_hud_and_wires():
    src = _read(_LIVE / "graphState.ts")
    assert "QUIET_DECAY_MS" in src
    assert "n.tok_s = 0;" in src, (
        "a node's rate stat must decay to 0 once its OWN last signal goes "
        "stale, or the HUD sum / wire-live check reads a session as "
        "permanently live forever after its last nonzero tick")


def test_self_heal_reconcile_catches_gate_flips_written_out_of_band():
    src = _read(_LIVE / "graphState.ts")
    assert "scheduleSelfHeal(" in src
    assert "private reconcile(" in src
    assert "setReconcileFetcher(" in src, (
        "GraphState must accept an injected fetcher so the page can wire "
        "the debounced self-heal refetch to /api/work/graph")
    page_src = _read(_LIVE_PAGE)
    assert "setReconcileFetcher(" in page_src


def test_tokens_turn_usd_total_ticks_the_spend_row():
    types_src = _read(_LIVE / "types.ts")
    assert "usd_total" in types_src
    state_src = _read(_LIVE / "graphState.ts")
    assert "event.usd_total" in state_src
    cards_src = _read(_LIVE / "cards.ts")
    assert "m.spendUsd" in cards_src, (
        "the Spend row must render a real per-node value, not the round1 "
        "hardcoded dead '$0'")


def test_layout_session_drop_is_floored_at_150px():
    src = _read(_LIVE / "layout.ts")
    assert "Math.max(150," in src, (
        "the driver-card-bottom -> session-card-top gap must be floored "
        "at 150px so the session->task wire is long enough for an "
        "in-transit marker to sit visibly mid-span (round1 gap: "
        "'wire run ~15-50px, markers flash sub-200ms')")


# ---------------------------------------------------------------------------
# Round 3 (gauntlet: camera auto-fit, icon vocabulary + legend, unmistakable
# markers + honest wire tint, real per-node throughput bar, log-scale HUD
# meter, graded silence, gate panel vs completion toasts, idle refinement).
# Pinned against E:\gamify-lab\R3_BRIEF.md's numbered fix list.
# ---------------------------------------------------------------------------

def test_camera_continuously_auto_fits_content_and_stands_down_for_user_input():
    state_src = _read(_LIVE / "graphState.ts")
    assert "private autoFitCamera(" in state_src, (
        "GraphState must own a continuous fit-to-content camera -- item 1's "
        "fix for the item-0 root cause (bootstrap() set pan/zoom ONCE and "
        "never touched it again, so any card born after boot rendered "
        "fully off-screen)")
    assert "AUTO_FIT_IDLE_MS" in state_src and "lastUserInputAt" in state_src, (
        "auto-fit must stand down for a while after the viewer's own "
        "pan/zoom/drag, never fighting their framing")
    assert "noteUserCameraInput(" in state_src
    assert "this.autoFitCamera(dtMs, now);" in state_src, (
        "auto-fit must run every step() frame, not just once at boot")

    page_src = _read(_LIVE_PAGE)
    assert page_src.count("noteUserCameraInput(") >= 2, (
        "LivePage must report BOTH drag-pan and wheel-zoom as user camera "
        "input, or auto-fit will fight a viewer who just zoomed in")


def test_node_icon_vocabulary_varies_by_role_and_a_legend_exists():
    palette_src = _read(_LIVE / "palette.ts")
    assert 'role === "qa"' in palette_src and 'role === "sm"' in palette_src, (
        "glyphFor must give a session/agent card a distinct icon per role "
        "(dev/qa/sm), not one generic dot for every agent (item 2)")

    hud_src = _read(_LIVE / "hud.ts")
    assert "export function drawLegend(" in hud_src, (
        "a small fixed legend chip naming the glyphs and state colors "
        "must exist (item 2: 'Small fixed legend chip (bottom-left)')")
    assert "LEGEND_STATES" in hud_src and "LEGEND_GLYPHS" in hud_src

    draw_src = _read(_LIVE / "draw.ts")
    assert "drawLegend(" in draw_src, "draw.ts must actually render the legend"


def test_token_wire_is_dim_neutral_until_flowing_not_always_teal():
    wires_src = _read(_LIVE / "wires.ts")
    # The item-0 root cause, pinned directly: wireColor used to return
    # PALETTE.teal unconditionally for a token-kind wire regardless of
    # `flowing`, differing only by ctx.globalAlpha in drawWire -- so an
    # idle wire and a busy wire were the SAME HUE, just dimmer (verified
    # live against a real scenario run). Now color itself branches on
    # `flowing` for BOTH wire kinds through one shared early-return.
    assert 'if (!flowing) return "rgba(255,255,255,0.16)";' in wires_src, (
        "an un-flowing wire (token OR structural) must render dim "
        "neutral, never a dimmed version of its flowing color")

    state_src = _read(_LIVE / "graphState.ts")
    assert "const SPAWN_COOLDOWN_MS = 600;" in state_src, (
        "item 3: hot wires must be able to spawn a marker up to every "
        "0.6s, not round2's 1.2s floor")

    packets_src = _read(_LIVE / "packets.ts")
    palette_src = _read(_LIVE / "palette.ts")
    assert "packetOutline" in palette_src and "packetOutline" in packets_src, (
        "item 3: the in-transit marker needs its own high-contrast "
        "outline color, distinct from the teal hue family the old "
        "packet color (#5eead4) shared with a live wire (#2dd4bf) -- "
        "low hue-contrast was part of why markers read as 'rare/subtle'")


def test_buffer_bar_is_a_per_card_rolling_peak_gauge_drawn_thicker():
    cards_src = _read(_LIVE / "cards.ts")
    assert "drawCapacityBar(ctx, labelX, rowY, barW, m.bufferFrac, PALETTE.teal, m.bufferFrac > 0.015, 7);" in cards_src, (
        "item 4: the Tokens flow-gauge bar must be 7px (thicker than the "
        "orange Step bar's 4px default), directly under the token value")

    state_src = _read(_LIVE / "graphState.ts")
    assert "BUFFER_PEAK_DECAY" in state_src and "lastTokenFlowAt" in state_src, (
        "the buffer bar must drain off SIGNAL-SPECIFIC silence "
        "(lastTokenFlowAt), never the general lastSignalAt a heartbeat "
        "alone would also bump")


def test_hud_hero_meter_is_log_scale_with_fixed_pips():
    hud_src = _read(_LIVE / "hud.ts")
    assert "export function logMeterFrac(" in hud_src, (
        "item 5: the hero tok/s meter needs a FIXED-anchor log scale so "
        "length is monotonic with the number regardless of what the "
        "recent history's own max happens to be")
    assert "LOG_METER_PIPS = [10, 100, 1_000, 10_000]" in hud_src, (
        "the fixed pips must be literally 10/100/1k/10k per the brief")
    assert "drawLogMeter(ctx, x, rowY + 30, meterW, totals.tokS, PALETTE.teal);" in hud_src, (
        "the hero row must actually use the log meter, not the old "
        "totals.tokS / recentMax(...) moving-target scale")


def test_connector_dots_fade_continuously_with_signal_age():
    state_src = _read(_LIVE / "graphState.ts")
    assert "export function signalFreshness(" in state_src, (
        "item 6: a card's connector dots must fade CONTINUOUSLY with "
        "signal age starting ~10s, not snap binarily at STALL_MS -- "
        "critic 3's gap: a 44s-quiet node was pixel-identical to a "
        "just-ticked one right up until the hard red flip")
    assert "FADE_START_MS = 10_000" in state_src

    cards_src = _read(_LIVE / "cards.ts")
    assert "signalFreshness(now, n.lastSignalAt)" in cards_src
    assert "since signal" in cards_src, (
        "item 6: a card must show a small 'Ns since signal' chip once "
        "its age crosses SIGNAL_AGE_CHIP_MS (15s)")


def test_gate_panel_is_a_persistent_docked_module_not_a_toast():
    gp_src = _read(_LIVE / "gatepanel.ts")
    assert "export function drawGatePanel(" in gp_src, (
        "item 7: a gate wait is an ONGOING state (can last minutes), not "
        "a momentary event -- it needs its own persistent docked panel, "
        "computed live from node state, never a spawned one-shot toast")
    assert "PALETTE.magenta" in gp_src

    draw_src = _read(_LIVE / "draw.ts")
    assert "drawGatePanel(ctx, state.nodes, now, width, height);" in draw_src


def test_quiet_line_distinguishes_needs_attention_from_genuinely_calm():
    idle_src = _read(_LIVE / "idle.ts")
    assert "needAttentionCount" in idle_src, (
        "item 8: 'queue is quiet' must read 'N need attention' when "
        ">=1 card is stalled/gated, and the plain last-activity line "
        "ONLY when nothing needs anyone -- critic 4's residual gap was "
        "'nothing separating one child stuck from system calm'")
    assert "need attention" in idle_src

    draw_src = _read(_LIVE / "draw.ts")
    assert "needAttentionCount" in draw_src, (
        "draw.ts must actually compute the count (stalled + "
        "waiting_gate cards) and pass it to drawQuietLine")
