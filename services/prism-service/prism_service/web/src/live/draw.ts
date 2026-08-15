/** THE render loop owner for /live: composes background, wires, packets,
 * cards and the HUD into one frame. Pure rendering — reads GraphState,
 * writes pixels, no physics or event handling (those live in
 * graphState.ts). LivePage.tsx's rAF loop calls state.step() then draw()
 * once per frame; nothing else drives motion (all state changes are
 * WorkEvent-sourced, so a still wire really means no flow). */

import type { GraphState, LiveNode } from "./graphState";
import { HEARTBEAT_DECAY_MS } from "./graphState";
import { drawCard, type CardMetrics } from "./cards";
import { drawWire, routeOrthogonal, type WireKind } from "./wires";
import { drawPackets } from "./packets";
import { drawHud } from "./hud";
import { drawLoading, drawQuietLine, isGraphQuiet } from "./idle";
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

  const queueDepth = state.edges.filter((e) => e.kind === "parent_of" && e.source === node.id).length;

  return {
    tokS, tokensTotal, tokensLive: anyLive,
    step: node.workflow_step, stepBarFrac: stepLive ? Math.max(heartbeatFrac, 0.08) : 0, stepLive,
    gatePending: node.gate_state === "pending",
    gateLabel: node.gate_state === "pending" ? "awaiting review" : "",
    queueDepth,
  };
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
    const live = kind === "token" ? !!a.tok_s && a.tok_s > 0 : true;
    const pts = routeOrthogonal(
      { x: a.x, y: a.y, w: a.slot.w, h: a.slot.h },
      { x: b.x, y: b.y, w: b.slot.w, h: b.slot.h },
    );
    drawWire(ctx, pts, kind, live);
  }
  drawPackets(ctx, state.packets);

  for (const n of state.nodes) {
    const m = metricsFor(n, state, now);
    drawCard(ctx, n, m, now);
  }

  ctx.restore();

  // HUD — fixed, screen space, independent of pan/zoom.
  drawHud(ctx, state, now);
  if (isGraphQuiet(state, now)) {
    drawQuietLine(ctx, 22, 148);
  }
}
