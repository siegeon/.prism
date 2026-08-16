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
        "scale.ts",
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
    # Round 4 item 2a SUPERSEDES round 3's per-card ROLLING PEAK gauge
    # (bufferPeak/bufferLevelFrac, current/peak) -- critic 5's "second
    # offense" caught the identical "rising peak shrinks a growing rate"
    # bug this round3 model was built to fix on the HUD side, still live
    # on every card's own bar. The bar now reads off scale.ts's shared
    # FIXED log scale (logMeterFrac), the same law the HUD hero meter
    # uses, so length is monotonic with the printed rate everywhere.
    assert "BUFFER_DRAIN_WINDOW_MS" in state_src, (
        "the buffer bar must drain to 0 over a real elapsed-time window "
        "since the last tokens.turn, not just ratchet up and sit at its "
        "high-water mark")
    assert "logMeterFrac(n.bufferLiveTokS)" in state_src, (
        "the buffer bar's fill must come from the SAME shared fixed log "
        "scale as the HUD hero meter (scale.ts), never a per-card "
        "rolling peak that a rising value can shrink against")
    assert "bufferPeak" not in state_src, (
        "round 4 item 2a retired the peak-relative gauge entirely -- "
        "a lingering bufferPeak field would mean the old bug is still "
        "reachable")


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
    assert int(m.group(1)) >= 7, (
        "round 4 must bump the gamify version marker to at least "
        f".7; got .{m.group(1)}")


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
    # Round 4 item 1c SUPERSEDES round1's unconditional literal: red is
    # now further gated on `graphQuiet` (critic 1's "red stacking on
    # every node with no recovery... reads as a system crash" -- see
    # test_stalled_red_is_contextual_on_whether_the_graph_is_quiet below
    # for the actual behavioral pin).
    assert 'if (state === "stalled") return graphQuiet ? PALETTE.textDim : PALETTE.red;' in palette_src
    # Every connector dot's dead-fallback must route through this one
    # function -- never a hardcoded PALETTE.red re-introduced on a row.
    assert "deadRingColorFor(state, graphQuiet)" in cards_src
    assert cards_src.count("drawConnectorDot(ctx, dotX, rowY,") >= 3, (
        "Tokens/Step/Spend rows must all pass through the same "
        "state-derived dead-ring color, not their own hardcoded fallback")


def test_stalled_red_is_contextual_on_whether_the_graph_is_quiet():
    # Round 4 item 1c: "their COLOR depends on context: red/warm only
    # when OTHER cards are actively receiving... neutral grey when the
    # whole queue is quiet". Pinned end to end: drawCard takes a
    # graphQuiet flag, the stalled top border reads it too (not just the
    # connector rings), and draw.ts actually threads the SAME
    # isGraphQuiet() result it already computes for the dim-alpha fix
    # through to drawCard, plus excludes a globally-quiet stall from the
    # "N need attention" count (a gate always still counts).
    cards_src = _read(_LIVE / "cards.ts")
    assert "graphQuiet" in cards_src
    assert 'ctx.strokeStyle = graphQuiet ? PALETTE.textDim : PALETTE.red;' in cards_src, (
        "the stalled card's top border must downgrade to neutral when "
        "the whole graph is quiet, not just the connector dot rings")

    draw_src = _read(_LIVE / "draw.ts")
    assert "drawCard(ctx, n, m, cardState, now, graphQuiet);" in draw_src, (
        "draw.ts must pass its own graphQuiet signal into drawCard")
    assert 'cardState === "stalled" && !graphQuiet' in draw_src, (
        "a stalled card must only inflate needAttentionCount while the "
        "graph is NOT globally quiet -- a gate ('waiting_gate') always "
        "counts regardless")


def test_waiting_gate_state_gets_magenta_stripe_distinct_from_stalled_red():
    src = _read(_LIVE / "cards.ts")
    assert 'state === "waiting_gate"' in src and 'state === "stalled"' in src
    assert "EDGE_STRIPE_W" in src and "PALETTE.magenta" in src, (
        "a gate-pending card must render a magenta left-edge stripe")
    # Round 5 item 5 trimmed the wash 0.4 -> 0.3 (stacks with the whole-
    # graph quiet dim in the film's final third; brief's floor is "any
    # card's effective alpha >= 0.6 at all times") -- the wash itself, not
    # its exact opacity, is what this test pins.
    assert "rgba(8,9,13,0.3)" in src, (
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
    # Round 5 item 1 SUPERSEDES round 3's continuous exponential-chase
    # call signature (`autoFitCamera(dtMs, now)` recomputing a target
    # every frame from raw dt): critics 1/2 (round 4) caught "the whole
    # view auto-scrolling/panning continuously instead of settling" --
    # the chase model never truly stops (asymptotic) and had no deadband,
    # so it kept re-aiming at every frame's float noise on top of item 0's
    # orphaned-session-slot bug feeding it a genuinely-changing bbox.
    # `autoFitCamera(now)` now only needs a clock, not a frame delta: it
    # compares each frame's bbox against the last COMMITTED fit with
    # AUTO_FIT_DEADBAND_PX slack, and any real change arms a FIXED
    # AUTO_FIT_MOVE_MS eased move that explicitly reaches its target and
    # holds -- see the deadband/fixed-move test below.
    assert "this.autoFitCamera(now);" in state_src, (
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

    # Round 4 item 2a retired BUFFER_PEAK_DECAY along with the rest of the
    # peak-relative model (see test_task_card_gets_a_throughput_buffer_bar_
    # that_drains above) -- lastTokenFlowAt is the part of this contract
    # that's still real: the bar drains off SIGNAL-SPECIFIC silence, never
    # the general lastSignalAt a heartbeat alone would also bump.
    state_src = _read(_LIVE / "graphState.ts")
    assert "lastTokenFlowAt" in state_src


def test_hud_hero_meter_is_log_scale_with_fixed_pips():
    # Round 4 item 2a moved the scale out of hud.ts into scale.ts, a
    # dedicated shared module, so cards.ts's per-node buffer bar can use
    # the EXACT same law (see test_buffer_bar_shares_the_hud_log_scale
    # below) instead of drifting into its own peak-relative model again
    # (critic 5's "second offense" against the same bug class).
    scale_src = _read(_LIVE / "scale.ts")
    assert "export function logMeterFrac(" in scale_src, (
        "item 5: the hero tok/s meter needs a FIXED-anchor log scale so "
        "length is monotonic with the number regardless of what the "
        "recent history's own max happens to be")
    assert "LOG_METER_PIPS = [10, 100, 1_000, 10_000]" in scale_src, (
        "the fixed pips must be literally 10/100/1k/10k per the brief")

    hud_src = _read(_LIVE / "hud.ts")
    assert 'from "./scale"' in hud_src, "hud.ts must import the shared scale, not redefine it"
    assert "drawLogMeter(ctx, x, rowY + 30, meterW, totals.tokS, PALETTE.teal);" in hud_src, (
        "the hero row must actually use the log meter, not the old "
        "totals.tokS / recentMax(...) moving-target scale")


def test_buffer_bar_shares_the_hud_log_scale():
    # Round 4 item 2a: "One shared scale util: the same fixed log scale
    # ... drives BOTH the HUD hero meter and every card's throughput
    # bar." Pinned as an actual shared import, not just two modules that
    # happen to compute the same formula independently (which is exactly
    # how round3's HUD fix and round2's per-card model drifted apart).
    scale_src = _read(_LIVE / "scale.ts")
    state_src = _read(_LIVE / "graphState.ts")
    hud_src = _read(_LIVE / "hud.ts")
    assert 'from "./scale"' in state_src, (
        "graphState.ts's buffer-bar fraction must import scale.ts's "
        "logMeterFrac rather than compute its own peak-relative ratio")
    assert 'import { logMeterFrac' in state_src or "logMeterFrac(" in state_src
    assert "export function logMeterFrac(" in scale_src
    assert 'from "./scale"' in hud_src


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


def test_subtasks_fan_multiple_columns_so_autofit_can_fill_a_landscape_viewport():
    src = _read(_LIVE / "layout.ts")
    # Residual item-1 fix found while verifying auto-fit against a real
    # scenario run: 3+ siblings under one parent all stacked at ONE
    # constant x built a tall narrow content bbox that auto-fit correctly
    # framed but still left huge side margins on a 1920x1080 viewport
    # (min(w/contentW, h/contentH) was dominated by the tall dimension).
    # Round 5 item 2 SUPERSEDES round3's fixed 2-column fan: reading the
    # actual FILM caught 5 siblings (2 never-advanced queued children +
    # 3 real subtasks, routine in this scenario) fanning into 3 rows at
    # 2 columns, and each row's session-height reserve (~239px) tripled
    # is what crashed autoFit's zoom ~0.85 -> ~0.46 the instant the 3rd
    # row appeared. SUB_COLS is now 3 -- a FIXED constant (deliberately
    # not recomputed from the running sibling count, which would
    # retroactively collide an earlier sibling's slot with a later one,
    # see placeSubtask's doc comment).
    assert "const SUB_COLS = 3;" in src
    assert "const col = idx % SUB_COLS;" in src and "const row = Math.floor(idx / SUB_COLS);" in src, (
        "subtasks of one parent must fan across SUB_COLS columns, not "
        "stack in a single column, so the content bbox isn't tall/narrow "
        "on a landscape viewport")


def test_reconcile_reclassifies_a_subtask_misplaced_as_a_root_task():
    layout_src = _read(_LIVE / "layout.ts")
    assert "reslotAsSubtask(id: string, parentTaskId: string): Slot" in layout_src, (
        "item-1 residual fix: every live SSE event handler's "
        "ensureTaskNode call omits kind/parentTaskId (WorkEvent carries "
        "only a bare task_id), so a subtask's first-seen live event "
        "routinely places it as an extra top-level task row instead of "
        "fanning beside its real parent -- verified live, this is why "
        "auto-fit still framed a tall narrow column on a 1920x1080 "
        "viewport even once the camera math itself was correct")

    state_src = _read(_LIVE / "graphState.ts")
    assert 'existing.kind === "task" && sn.kind === "subtask"' in state_src, (
        "GraphState.reconcile() must detect and correct this "
        "misclassification the moment a self-heal snapshot reveals the "
        "node's true kind/parent")
    assert "this.layout.reslotAsSubtask(sn.id, parentId)" in state_src


# ---------------------------------------------------------------------------
# Round 4 (gauntlet: quiet-dim blackout root cause, throughput bar
# honesty second offense, queue glyph, no bare hex ids, marker comet
# tail, selection outline). Pinned against E:\gamify-lab\R4_BRIEF.md's
# numbered fix list. PROTECT THE WINS: piece 2 (edge motion) and piece 4
# (idle) won blind in round 3 -- every assertion below is additive to
# those, never a removal of the ticking signal-age chip, the quiet line,
# or the scrolling sparkline.
# ---------------------------------------------------------------------------

def test_quiet_dim_eases_in_instead_of_snapping():
    # Item 1a root cause (see the task's final report for the full
    # evidence chain): the 0.85 dim FACTOR itself was already correct
    # (never the inverted 0.15) -- the bug was a hard boolean snap
    # landing on every card in the same frame, which read as a
    # synchronized "collapse" (critic 3, pixel-verified). idle.ts now
    # exposes a continuous quietDimAlpha() and draw.ts uses ITS result
    # (not a bare isGraphQuiet() ? 0.85 : 1 ternary) for globalAlpha.
    idle_src = _read(_LIVE / "idle.ts")
    assert "export function quietDimAlpha(" in idle_src
    assert "QUIET_DIM_FLOOR = 0.85" in idle_src, (
        "the dim floor must stay ~15% dimming (alpha ~0.85), never the "
        "inverted 0.15 the brief asked us to rule out")
    assert "EASE_MS" in idle_src, (
        "the dim must ease in over ~2s, not snap in one step")

    draw_src = _read(_LIVE / "draw.ts")
    assert "quietDimAlpha(state, now)" in draw_src
    # Round 5 item 5 SUPERSEDES the bare assignment with an explicit
    # Math.max(0.6, dimAlpha) floor -- dimAlpha itself is still exactly
    # this eased quietDimAlpha() result, just guarded so a future
    # per-node dim stacked on top can never push a card's effective
    # alpha below what a title stays legibly readable at.
    assert "ctx.globalAlpha = Math.max(0.6, dimAlpha);" in draw_src, (
        "draw.ts must apply the eased alpha (floored at 0.6), not "
        "re-derive its own binary 0.85-or-1 snap")


def test_hud_cumulative_counts_never_zero_only_rates_do():
    # Item 1b root cause: the HUD's hero number used to BE the decaying
    # tok/s RATE (totals.tokS), so the single most visually dominant
    # value on the canvas crashed to 0 the instant flow paused -- read by
    # critics as "tasks 1.5K->0, sessions 2->0" collapsing. tokensTotal
    # (summed from each session's own monotonic tokens_total) and
    # agentsTotal (every session-kind node present, not just ones
    # mid-tick) are the new headline values; only the genuinely-rate
    # fields (tokS, agentsLiveNow, gatesWaiting) are allowed to read 0.
    hud_src = _read(_LIVE / "hud.ts")
    assert "tokensTotal: number;" in hud_src, (
        "HudTotals needs a cumulative tokens-total field distinct from "
        "the tok/s rate")
    assert "agentsTotal += 1;" in hud_src, (
        "agentsTotal must count every session-kind node present, not "
        "just ones with tok_s>0 right now")
    assert "tasksInFlight += 1;" in hud_src, (
        "a cumulative tasks-in-flight count must exist so the combined "
        "'tasks in flight / gates waiting' row (design directive) never "
        "reads as a crashing task counter")
    # The hero row's BIG number is the cumulative total; the RATE is
    # relegated to the small dim sub-label where reading '0/s' is honest,
    # not alarming.
    assert 'drawGhostable(\n    ctx, `${fmtBig(totals.tokensTotal)}`' in hud_src or (
        "fmtBig(totals.tokensTotal)" in hud_src
    ), "the hero row's big value must be the cumulative total, not the rate"


def test_agents_row_shades_live_vs_present_without_zeroing_the_count():
    # Item 1b: the segmented meter must distinguish "how many agents
    # exist" (never zeroes) from "how many are producing right now" (a
    # genuine rate, allowed to zero) via shading, not by dropping the
    # count itself.
    hud_src = _read(_LIVE / "hud.ts")
    assert "liveN?: number" in hud_src or "liveN?:" in hud_src, (
        "drawSegments must accept an optional live-count so the agents "
        "row can shade present-but-quiet units distinctly from the "
        "currently-transmitting ones")
    assert "totals.agentsTotal, 12, PALETTE.teal, totals.agentsLiveNow" in hud_src


def test_contextual_signal_chip_color_never_reads_as_mass_crash():
    # Item 1c: covered end-to-end by
    # test_stalled_red_is_contextual_on_whether_the_graph_is_quiet above
    # (palette.ts + cards.ts + draw.ts). This test additionally protects
    # the win: the ticking "Ns since signal" chip TEXT itself (critic 4
    # loved it) must keep rendering unconditionally -- round 4 only
    # recolors the RING/BORDER around it, never removes or mutes the
    # chip.
    cards_src = _read(_LIVE / "cards.ts")
    assert "since signal" in cards_src
    assert "SIGNAL_AGE_CHIP_MS" in cards_src


def test_queue_glyph_caps_with_overflow_count():
    # Item 3: "1 square per queued child, cap ~5 with a +N".
    cards_src = _read(_LIVE / "cards.ts")
    assert "QUEUE_GLYPH_CAP = 5" in cards_src
    assert "m.queueDepth > QUEUE_GLYPH_CAP" in cards_src, (
        "queueDepth beyond the cap must render a '+N' overflow caption, "
        "not silently stop at the cap with no indication more exist")


def test_scenario_seeds_permanently_queued_children_for_the_glyph():
    # Item 3 (rig, not product): scenario.py must give the root task 2
    # children that stay pending/never-advanced the whole movie so
    # queue_depth > 0 is actually witnessable on film (api/work.py's
    # _queue_depth counts pending children with no workflow_step).
    scenario_src = (Path(r"E:\gamify-lab\sim\scenario.py")).read_text(encoding="utf-8")
    assert "queued" in scenario_src.lower()
    assert "set_parent(" in scenario_src


def test_no_bare_hex_id_labels_ever():
    # Item 4: session cards and task/subtask placeholders used to label
    # themselves `id.slice(0, 8)` -- a raw hex fragment -- until a later
    # event happened to backfill something readable. Now both fall back
    # to human text (role · driven-task-title, or a plain "starting"
    # caption), never the id.
    state_src = _read(_LIVE / "graphState.ts")
    assert "id.slice(0, 8)" not in state_src, (
        "no node label may ever fall back to a raw id fragment")
    assert "function sessionLabelFor(" in state_src
    assert "function placeholderTaskLabel(" in state_src


def test_marker_comet_tail_shows_travel_direction():
    # Item 5: "give markers a short comet tail (2-3 fading trail dots)
    # indicating travel direction" -- protecting piece 2's win (the
    # marker itself, its cadence, its outline) while fixing the residual
    # ambiguity on a vertical wire.
    packets_src = _read(_LIVE / "packets.ts")
    assert "TRAIL_STEPS" in packets_src
    assert "p.t - i * TRAIL_T_GAP" in packets_src, (
        "trail dots must sit BEHIND the head along the same polyline "
        "fraction, not be drawn as a generic glow")


def test_marker_flicker_fixed_by_topping_up_flowing_wires():
    # Item 5's other residual: "marker visibility flickers off in
    # roughly 1 of 3 sampled frames on a busy wire". Root cause: real
    # tokens.turn events arrive about once a second while a short wire's
    # travel time can be shorter than that, so the wire legitimately
    # empties between spawns. Fixed by topping up any wire graphState
    # already knows is flowing (isEdgeFlowing, itself only ever set by
    # real events) the moment it has zero packets in flight -- never a
    # bare timer manufacturing motion.
    state_src = _read(_LIVE / "graphState.ts")
    assert "private ensureFlowingWiresCarryAMarker(" in state_src
    assert "this.ensureFlowingWiresCarryAMarker(now);" in state_src, (
        "step() must actually call the top-up every frame")
    assert "this.isEdgeFlowing(e.source, e.target, now)" in state_src, (
        "the top-up must gate on the real recent-flow signal, never "
        "spawn on a wire that hasn't actually carried flow")


def test_selection_gets_a_lift_in_addition_to_the_outline():
    # Item 6: "steady orange 2px outline + slight card lift" -- the
    # outline alone was already pinned (round1); this adds the lift.
    cards_src = _read(_LIVE / "cards.ts")
    assert "const lifted = n.selected;" in cards_src
    assert "ctx.translate(0, -3);" in cards_src, (
        "a selected card must visually lift a few px, not just gain an "
        "outline in place")
    assert "ctx.shadowBlur" in cards_src, (
        "the lift should read as elevation (a soft shadow), not just a "
        "silent position shift")


def test_session_cards_get_the_same_throughput_bar_as_tasks():
    # Item 2b: "the fastest-moving leaf nodes carry no bar at all" -- the
    # old `if (n.kind !== "session")` guard around the buffer-bar block
    # is gone, and draw.ts's per-session CardMetrics now reads the node's
    # real bufferFrac instead of hardcoding 0.
    cards_src = _read(_LIVE / "cards.ts")
    assert 'if (n.kind !== "session") {\n    const barW' not in cards_src, (
        "the buffer bar block must no longer exclude session cards")
    draw_src = _read(_LIVE / "draw.ts")
    assert "bufferFrac: node.bufferFrac," in draw_src, (
        "a session card's CardMetrics.bufferFrac must read the node's "
        "real field, not a hardcoded 0")


# ---------------------------------------------------------------------------
# Round 5 (gauntlet: recover piece 2's regression, win piece 1, protect
# 3/4/5). Pinned against E:\gamify-lab\R5_BRIEF.md's numbered fix list.
# Item 0's root cause (see the task's final report for the full live
# evidence chain): a session card's label/position were captured ONCE at
# creation/first-role-event from its driver task, which could still be a
# placeholder at that instant during a staggered fan-out -- if the
# session then went idle before a later event could re-derive them, both
# stayed frozen (a literal "task starting..." caption, and a wire that no
# longer lined up with its reslotted driver) for the rest of the film.
# ---------------------------------------------------------------------------

def test_session_label_lives_off_its_driver_every_frame_not_frozen_once():
    state_src = _read(_LIVE / "graphState.ts")
    assert "labelIsPlaceholder: boolean;" in state_src, (
        "a LiveNode must track whether its CURRENT label is a "
        "driver-echo guess (placeholder) or a real backend record, so "
        "step() knows which nodes still need live-refreshing")
    assert 'if (n.kind === "session" && n.labelIsPlaceholder && n.driverOfId)' in state_src, (
        "step() must re-derive a placeholder session's label from its "
        "LIVE driver EVERY frame -- the round4 bug was that this only "
        "ever happened once (creation, or the next agent.run), so a "
        "session that went idle before its driver's own title healed "
        "froze on the stale guess forever")
    assert "node.labelIsPlaceholder = true;" in state_src, (
        "both lazy-create paths (upsertSessionNode, ensureTaskNode) must "
        "mark their guessed label as a placeholder")
    assert "existing.labelIsPlaceholder = false;" in state_src, (
        "reconcile() must flip a node's label back to authoritative the "
        "moment a real backend label lands, so the live-echo loop never "
        "clobbers a real 'role · model' session label afterward")


def test_reconcile_first_discovery_applies_the_real_label_immediately():
    # Round4's reconcile() `if (!existing) { ensureTaskNode(...); continue; }`
    # created a task/subtask node on first discovery WITHOUT ever reading
    # sn.label -- so a node reconcile discovers before any live event ever
    # touched it kept ensureTaskNode's placeholder text until some OTHER
    # unrelated task.changed happened to re-trigger self-heal, which could
    # be many seconds away. Fixed: apply sn.label right at discovery.
    state_src = _read(_LIVE / "graphState.ts")
    assert "if (created && sn.label) {" in state_src
    assert "created.labelIsPlaceholder = false;" in state_src


def test_reslot_also_carries_along_any_already_placed_session_cards():
    # Item 0 part B: reslotAsSubtask deletes and re-places the TASK's own
    # slot but never touched sessSlot for a session already anchored to
    # the task's OLD (wrong, root-column) position -- verified live, this
    # produced a session card with no wire lining up to its (moved)
    # driver at all ("some visually-adjacent cards have no connecting
    # wire drawn").
    layout_src = _read(_LIVE / "layout.ts")
    assert "reslotSessionsOf(driverId: string): { id: string; slot: Slot }[]" in layout_src, (
        "LayoutEngine needs a way to recompute every session anchored to "
        "a driver against that driver's NEW slot")
    assert "private sessionSlotAt(driverSlot: Slot, idx: number): Slot" in layout_src, (
        "placeSession and reslotSessionsOf must share ONE slot formula "
        "so they can never drift apart")

    state_src = _read(_LIVE / "graphState.ts")
    assert "this.layout.reslotSessionsOf(sn.id)" in state_src, (
        "reconcile()'s reslot branch must call this right after "
        "reslotAsSubtask, in the same breath the task itself moves")
    assert "sessNode.slot = newSessSlot.slot;" in state_src, (
        "the session LiveNode's own slot must actually be updated (and "
        "a fresh spawn-in armed) so it visually follows its driver")


def test_camera_uses_a_deadband_and_a_fixed_duration_move_not_a_chase():
    # Item 1: critics 1/2 caught "the whole view auto-scrolling/panning
    # (~80-100px per 0.2s frame)" -- round3's model recomputed a target
    # from raw node.slot data EVERY FRAME with no deadband and eased
    # toward it asymptotically (never truly settles). Now a materially-
    # unchanged bbox (within AUTO_FIT_DEADBAND_PX) is a no-op, and a real
    # change arms ONE fixed AUTO_FIT_MOVE_MS eased move that reaches its
    # target and then holds exactly -- pixel-stable between topology
    # events, per the film protocol's item (c).
    state_src = _read(_LIVE / "graphState.ts")
    assert "const AUTO_FIT_DEADBAND_PX = 24;" in state_src
    assert "const AUTO_FIT_MOVE_MS = 750;" in state_src
    assert "const materiallyChanged = this.fitBoundsMinX === null" in state_src
    assert "if (elapsed >= AUTO_FIT_MOVE_MS) {" in state_src, (
        "the move must have an explicit end -- HOLD exactly at the "
        "target once reached, not keep asymptotically chasing forever")
    assert "this.fitBoundsMinX = null;" in state_src, (
        "bootstrap() must reset the committed fit so a fresh page load "
        "arms an immediate first move instead of comparing against a "
        "stale bbox from a prior project/page")


def test_marker_gets_a_teal_halo_behind_its_bright_core():
    # Item 3 (recover piece 2's win): the design directive asks for
    # "Bright solid core (near-white center, teal halo)" -- the halo was
    # simply never drawn; critics needed +0.15 brightness/1.6x contrast/
    # 4-15x upscale to see the marker at native brightness at all.
    packets_src = _read(_LIVE / "packets.ts")
    assert "ctx.fillStyle = PALETTE.teal;" in packets_src, (
        "the marker head must paint a teal halo, not just the near-white "
        "core + dark outline")
    # Round 5 item 3 residual: a live browser capture proved the core
    # paints the exact right pixels -- the gap is record_ours.ps1's
    # libx264 veryfast encode crushing a small 7x7 feature. Sized up so
    # the encoder has more contiguous high-contrast area to keep;
    # verified by re-filming and scanning extracted frames for
    # near-white pixels (see the task's final report).
    assert "ctx.arc(at.x, at.y, 10, 0, Math.PI * 2);" in packets_src, (
        "the halo radius must be sized up from round4's 7px")
    assert "ctx.globalAlpha = 0.75;" in packets_src
    assert "ctx.rect(at.x - 4.5, at.y - 4.5, 9, 9);" in packets_src, (
        "the core must be sized up from round4's 7x7")


def test_legend_done_green_is_hue_distinct_from_working_teal():
    # Item 4 (protect piece 3): critic sampled working vs done swatches
    # as "nearly identical teal-green (18,87,75 vs 31,79,67)" -- #34d399
    # (hue ~160) sat only 12 degrees from teal's #2dd4bf (hue ~172).
    # #22c55e (hue ~142) is a meaningfully more pure green, 30 degrees
    # from teal, while staying unambiguously "green" for the locked
    # money/passed-gate meaning.
    palette_src = _read(_LIVE / "palette.ts")
    assert 'green: "#22c55e",' in palette_src
    assert 'teal: "#2dd4bf",' in palette_src, (
        "teal itself must be untouched -- only done's green moved")


def test_quiet_card_alpha_floors_at_point_six():
    # Item 5: "any card's effective alpha >= 0.6 at all times" -- an
    # explicit hard clamp at the one call site that sets globalAlpha for
    # every card's draw, so a future per-node dim added there can never
    # silently stack the whole-graph quiet dim (idle.ts's 0.85 floor)
    # below what a title stays legibly readable at.
    draw_src = _read(_LIVE / "draw.ts")
    assert "ctx.globalAlpha = Math.max(0.6, dimAlpha);" in draw_src


def test_hud_hero_card_carries_its_own_last_activity_line_when_quiet():
    # Item 6: critic 4 asked for a small first-class "last activity Xs
    # ago" line ON THE HUD CARD ITSELF during quiet -- previously this
    # only existed on the separate docked quiet-line (idle.ts), which
    # reads as the gate toast doing the work, not the HUD.
    hud_src = _read(_LIVE / "hud.ts")
    assert "isGraphQuiet(state, now)" in hud_src, (
        "the HUD hero row must know whether the graph reads quiet")
    assert "quiet ${Math.max(0, Math.floor((now - state.lastEventAt) / 1000))}s" in hud_src, (
        "the hero row's own rate sub-label must carry a ticking "
        "last-activity counter during quiet, not just the docked "
        "quiet-line")


def test_steward_glyph_is_not_confusable_with_the_task_square():
    # Found reading the actual FILM (not a single-frame check): a root
    # task's own steward session sat directly under the root card with
    # an all-but-identical filled-square glyph ("■" steward vs "▣" task)
    # at card-title size under video compression -- read as a duplicated
    # card at a glance even though the wire/data were correct.
    palette_src = _read(_LIVE / "palette.ts")
    assert 'if (role === "sm") return "▼";' in palette_src, (
        "the steward glyph must share no silhouette with the task glyph "
        "'▣' -- a triangle (pairing naturally with qa's '▲') reads "
        "distinctly at small size where two filled squares do not")


def test_session_label_leads_with_role_once_known_not_truncated_title_echo():
    # Found reading the actual FILM: every scenario title in the movie
    # runs 30-47 chars, well past cards.ts's ~30-char flat truncation
    # budget, so round4's `${driverLabel} · ${roleWord}` shape NEVER
    # actually showed the role suffix -- a session read as a plain
    # (truncated) copy of its driver's own title. VISUAL_GRAMMAR.md's
    # spec for this card is "model+role title", not an echo of the
    # driven task's title.
    state_src = _read(_LIVE / "graphState.ts")
    assert 'if (roleWord) return `${roleWord} agent`;' in state_src, (
        "once role is known, the session label must lead with it in a "
        "short form that always fits inside the card's title budget")


def test_version_bumped_past_round_5():
    import re

    ver_src = _read(_HERE.parent.parent.parent / "prism_service" / "__version__.py")
    m = re.search(r'PRISM_VERSION = "7\.10\.54\+gamify\.(\d+)"', ver_src)
    assert m, (
        "PRISM_VERSION must stay a readable 7.10.54+gamify.N marker; "
        f"got a version line that doesn't match: {ver_src.splitlines()[:1]!r}")
    assert int(m.group(1)) >= 8, (
        "round 5 must bump the gamify version marker to at least "
        f".8; got .{m.group(1)}")
