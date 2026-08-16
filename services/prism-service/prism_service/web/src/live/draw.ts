/** THE render loop owner for /live: composes background, wires, packets,
 * cards and the HUD into one frame. Pure rendering — reads GraphState,
 * writes pixels, no physics or event handling (those live in
 * graphState.ts). LivePage.tsx's rAF loop calls state.step() then draw()
 * once per frame; nothing else drives motion (all state changes are
 * WorkEvent-sourced, so a still wire really means no flow). */

import type { GraphState, LiveNode } from "./graphState";
import { HEARTBEAT_DECAY_MS, deriveCardState } from "./graphState";
import { drawCard, type CardMetrics } from "./cards";
import { drawWire, routeOrthogonal, type WireKind } from "./wires";
import { drawPackets } from "./packets";
import { drawHud, drawLegend } from "./hud";
import { drawLoading, drawQuietLine, isGraphQuiet, quietDimAlpha } from "./idle";
import { drawToasts } from "./toasts";
import { drawGatePanel } from "./gatepanel";
import { PALETTE } from "./palette";

function drawGrid(ctx: CanvasRenderingContext2D, w: number, h: number, pan: { x: number; y: number }, zoom: number): void {
  ctx.fillStyle = PALETTE.ground;
  ctx.fillRect(0, 0, w, h);
  const step = 28 * zoom;
  if (step < 6) return;
  const offX = (-pan.x * zoom) % step;
  const offY = (-pan.y * zoom) % step;
  ctx.fillStyle = PALETTE.grid;
  for (let x = offX; x < w; x += step) {
    for (let y = offY; y < h; y += step) {
      ctx.fillRect(x, y, 1.4, 1.4);
    }
  }
}

function edgeKind(k: "parent_of" | "driven_in"): WireKind {
  return k === "driven_in" ? "token" : "structure";
}

/** Roll a task/subtask card's own token stat up from every session
 * driven_in to it — a fan-out task shows the SUM of its agents' output,
 * not a blank row, even though the backend only stamps tok_s on session
 * nodes today. */
function metricsFor(node: LiveNode, state: GraphState, now: number): CardMetrics {
  if (node.kind === "session") {
    const live = !!node.tok_s && node.tok_s > 0;
    return {
      tokS: node.tok_s, tokensTotal: node.tokens_total, tokensLive: live,
      step: "", stepBarFrac: 0, stepLive: false,
      gatePending: false, gateLabel: "", queueDepth: 0,
      // Round 4 item 2b: a session card gets the same throughput bar as
      // a task/subtask, sourced from its OWN bufferFrac (computed
      // uniformly for every node kind in graphState.step()).
      bufferFrac: node.bufferFrac,
      spendUsd: 0,
    };
  }

  let tokS = 0, tokensTotal = 0, anyLive = false;
  for (const e of state.edges) {
    if (e.kind !== "driven_in" || e.target !== node.id) continue;
    const sess = state.nodes.find((n) => n.id === e.source);
    if (!sess) continue;
    if (sess.tok_s && sess.tok_s > 0) anyLive = true;
    tokS += sess.tok_s || 0;
    tokensTotal += sess.tokens_total || 0;
  }

  const heartbeatFrac = node.lastHeartbeatAt
    ? Math.max(0, 1 - (now - node.lastHeartbeatAt) / HEARTBEAT_DECAY_MS)
    : 0;
  const stepLive = node.status === "in_progress" && !!node.workflow_step;

  // Real backed-up-children count from the backend (api/work.py's
  // queue_depth: pending children that never entered the conductor) when
  // the boot/reconcile snapshot has populated it; falls back to the raw
  // parent_of edge count for a placeholder node reconcile hasn't
  // backfilled yet, same as round 1's approximation.
  const queueDepth = node.queue_depth
    || state.edges.filter((e) => e.kind === "parent_of" && e.source === node.id).length;

  // Trust gate_state alone, never gatePendingSince's truthiness: 0 (or
  // any negative value, now that graphState no longer clamps it to 0)
  // is a legitimate real timestamp, not an "unset" sentinel.
  const gateWaitS = node.gate_state === "pending"
    ? (now - node.gatePendingSince) / 1000
    : null;
  const gateLabel = gateWaitS != null
    ? `${node.workflow_step || "gate"} · waiting ${fmtWaitingMins(gateWaitS)}`
    : "";

  return {
    tokS, tokensTotal, tokensLive: anyLive,
    step: node.workflow_step, stepBarFrac: stepLive ? Math.max(heartbeatFrac, 0.08) : 0, stepLive,
    gatePending: node.gate_state === "pending",
    gateLabel,
    queueDepth, bufferFrac: node.bufferFrac,
    spendUsd: node.spend_usd || 0,
  };
}

/** "waiting Xm" per the build directive; sub-minute waits read as "<1m"
 * rather than "0m", which would misread as "not actually waiting". */
function fmtWaitingMins(waitS: number): string {
  const m = Math.floor(waitS / 60);
  return m < 1 ? "<1m" : `${m}m`;
}

export function draw(ctx: CanvasRenderingContext2D, state: GraphState, now: number, version: string): void {
  const { width, height } = state;
  // Deliberately no setTransform() here: LivePage's ResizeObserver sets a
  // devicePixelRatio scale ONCE on the context and this function never
  // touches it, so clearRect/fillRect in CSS-pixel units below still hit
  // the full hi-DPI backing store. save()/restore() around the pan/zoom
  // block compose on top of that base transform, not over it.
  ctx.clearRect(0, 0, width, height);

  drawGrid(ctx, width, height, state.pan, state.zoom);

  if (!state.booted) {
    drawLoading(ctx, width, height, now, version);
    return;
  }

  ctx.save();
  ctx.translate(-state.pan.x * state.zoom, -state.pan.y * state.zoom);
  ctx.scale(state.zoom, state.zoom);

  // Wires + packets, under cards.
  for (const e of state.edges) {
    const a = state.nodes.find((n) => n.id === e.source);
    const b = state.nodes.find((n) => n.id === e.target);
    if (!a || !b) continue;
    const kind = edgeKind(e.kind);
    // Token wires read "flowing" off the session's own live tok_s;
    // structural wires read the ~4s propagated-flow window graphState
    // tracks per edge (piece 2: a structural edge tints teal only while
    // real motion has actually propagated up it, never permanently).
    const live = kind === "token" ? !!a.tok_s && a.tok_s > 0 : state.isEdgeFlowing(e.source, e.target, now);
    const pts = routeOrthogonal(
      { x: a.x, y: a.y, w: a.slot.w, h: a.slot.h },
      { x: b.x, y: b.y, w: b.slot.w, h: b.slot.h },
    );
    drawWire(ctx, pts, kind, live);
  }
  drawPackets(ctx, state.packets);

  // Round 2, piece 4 (honest idle): when the WHOLE graph has been quiet
  // for a while, every card dims ~15% -- never blanked, never removed,
  // last real numbers stay exactly as they were (build item 3: "cards
  // keep their last real numbers, dim ~15%, never blanked"). A single
  // card's own STALLED state (piece 3) is a separate, per-node signal
  // (red rings) layered on top of this, not replaced by it. Round 4 item
  // 1a: the alpha now EASES in over ~2s via idle.ts's quietDimAlpha
  // instead of snapping in one frame the instant QUIET_MS elapses -- the
  // snap landing on every card simultaneously is what critic 3 pixel-
  // verified as a synchronized "collapse".
  const graphQuiet = isGraphQuiet(state, now);
  const dimAlpha = quietDimAlpha(state, now);
  // Round 3 item 8 (idle refinement): counted alongside the per-node
  // cardState computation every frame is already doing (never a second
  // pass) -- feeds the quiet line's "N need attention" branch below.
  // Round 4 item 1c: a STALLED card only "needs attention" while other
  // cards are still active -- once the whole graph is quiet, a stopped
  // signal is expected (everything stopped together), not an anomaly, so
  // it no longer inflates the count. A pending GATE always needs a
  // decision regardless of how quiet the rest of the graph is.
  let needAttentionCount = 0;
  for (const n of state.nodes) {
    const m = metricsFor(n, state, now);
    let cardState = deriveCardState(n, now);
    // A session card carries no `status` of its own (upsertSessionNode
    // always seeds ""), so deriveCardState can only ever age it into
    // STALLED, never DONE -- verified live: the session that drove an
    // already-completed task read alarm-red ~25s after the task itself
    // had already settled to its green chip, which misreads as "this
    // agent failed" rather than "this agent finished". If the task it
    // drove is done, the session inherits that done-ness instead.
    if (n.kind === "session" && cardState === "stalled" && n.driverOfId) {
      const driver = state.nodes.find((d) => d.id === n.driverOfId);
      if (driver && driver.status === "done") cardState = "done";
    }
    if (cardState === "waiting_gate") needAttentionCount += 1;
    else if (cardState === "stalled" && !graphQuiet) needAttentionCount += 1;
    ctx.save();
    ctx.globalAlpha = dimAlpha;
    drawCard(ctx, n, m, cardState, now, graphQuiet);
    ctx.restore();
  }

  ctx.restore();

  // HUD — fixed, screen space, independent of pan/zoom.
  drawHud(ctx, state, now);
  if (graphQuiet) {
    // Sits in the ~22px gap between the HUD panel's bottom edge (~206,
    // see hud.ts's PANEL_W/panelH math) and layout.ts's ORIGIN_Y (228,
    // where the first task card starts) -- verified against a live
    // screenshot that y=232 visually collided with the top card's title
    // bar text; y=217 clears both.
    drawQuietLine(ctx, 22, 217, (now - state.lastEventAt) / 1000, needAttentionCount);
  }

  // Legend chip -- fixed, screen space, bottom-left (round 3 item 2).
  drawLegend(ctx, 22, height - 14);

  // Persistent gate-wait panel -- fixed, screen space, right edge (round
  // 3 item 7). Drawn BEFORE the completion toasts so a toast sliding in
  // over the bottom-right corner never gets clipped under it.
  drawGatePanel(ctx, state.nodes, now, width, height);

  // Docked completion toasts -- screen space, independent of pan/zoom,
  // drawn last so they sit above everything.
  drawToasts(ctx, state.toasts, width, height, now);
}
