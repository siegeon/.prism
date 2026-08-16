/** Fixed HUD overlay, top-left, independent of pan/zoom (design
 * directive). Round 2 rebuild (piece 5 critic: "at 25% downscale [the
 * old HUD] reads as mostly empty dark canvas with one small unreadable
 * text cluster in the corner" while the reference game "still reads as
 * a dense column of load-bearing texture"): tok/s is now a genuine hero
 * number, every stat row carries its own meter (a continuous bar for
 * tok/s and spend rate, segmented blocks for agents-live/gates-waiting
 * counts) so the panel reads as INSTRUMENTATION even blurred, the
 * sparkline is taller with a filled area + emphasized endpoint dot, and
 * every value ghosts on change (cards.ts's drawGhostable, reused here
 * per the build directive rather than reinvented). */

import type { GraphState } from "./graphState";
import { PALETTE, glyphFor } from "./palette";
import { drawGhostable } from "./cards";
import { logMeterFrac, logMeterPipFrac, LOG_METER_PIPS } from "./scale";

const PAD = 14;
const PANEL_W = 320;
const HERO_H = 54;
const STAT_H = 40;
const SPARK_H = 46;
const GHOST_MS = 650;

/** Round 4 item 1b (quiet-dim blackout root cause #2): a HUD row's BIG
 * headline value must be a value that only moves on a real state change
 * -- never a decaying RATE. Round1-3's hero row put the raw tok/s RATE
 * (totals.tokS, decayed to 0 by graphState's QUIET_DECAY_MS) in the big
 * 26px slot, so the single most visually dominant number on the whole
 * canvas crashed to 0 the instant flow paused -- critic 3's "tasks
 * 1.5K->0, sessions 2->0" reads as exactly this (a big corner number
 * hitting zero), and critic 1's "reads as a system crash" is the same
 * root cause from a different angle. `tokensTotal` (summed from each
 * session's own monotonic tokens_total counter) and `agentsTotal` (every
 * session-kind node currently on the graph, not just ones mid-tick) never
 * drop just because the queue went quiet -- only genuinely RATE-shaped
 * numbers (tokS, agentsLiveNow, gatesWaiting) are allowed to read 0. */
export type HudTotals = {
  /** Cumulative sum of every session's own tokens_total -- monotonic,
   * never decays. This is the hero row's BIG number now. */
  tokensTotal: number;
  /** Aggregate rate across live sessions -- legitimately 0 when quiet;
   * shown as a small dim sub-label AND still drives the log meter (the
   * bar honestly reads "nothing flowing right now" while the headline
   * number keeps the session's real total in view). */
  tokS: number;
  spendUsd: number;
  /** Every session-kind node present on the graph, live or gone quiet --
   * cumulative, never zeroes just because nobody's mid-tick right now. */
  agentsTotal: number;
  /** Subset of agentsTotal currently emitting tok_s>0 -- a genuine RATE,
   * allowed to read 0. Drives the segmented meter's bright-vs-dim split. */
  agentsLiveNow: number;
  /** Task/subtask nodes not yet done/cancelled -- cumulative, never
   * zeroes from mere silence (item 1b: "tasks count... never zero"). */
  tasksInFlight: number;
  gatesWaiting: number;
};

export function computeHudTotals(state: GraphState): HudTotals {
  let tokensTotal = 0;
  let tokS = 0;
  let spendUsd = 0;
  let agentsTotal = 0;
  let agentsLiveNow = 0;
  let tasksInFlight = 0;
  let gatesWaiting = 0;
  for (const n of state.nodes) {
    if (n.kind === "session") {
      agentsTotal += 1;
      tokensTotal += n.tokens_total || 0;
      if (n.tok_s && n.tok_s > 0) agentsLiveNow += 1;
      tokS += n.tok_s || 0;
    } else {
      if (n.gate_state === "pending") gatesWaiting += 1;
      if (n.status !== "done" && n.status !== "cancelled") tasksInFlight += 1;
      spendUsd += n.spend_usd || 0;
    }
  }
  return { tokensTotal, tokS, spendUsd, agentsTotal, agentsLiveNow, tasksInFlight, gatesWaiting };
}

// Module-scope ghost bookkeeping: the HUD's numbers are derived
// aggregates with no natural "node" to hang a ghostUntil timestamp on
// (unlike cards.ts), so a small closure-local record does the same job.
let prevTotals: HudTotals | null = null;
const ghostUntil = { tokensTotal: 0, spendUsd: 0, agentsTotal: 0, tasksGates: 0 };

function updateGhosts(t: HudTotals, now: number): void {
  if (prevTotals) {
    if (Math.round(t.tokensTotal) !== Math.round(prevTotals.tokensTotal)) ghostUntil.tokensTotal = now + GHOST_MS;
    if (t.spendUsd.toFixed(2) !== prevTotals.spendUsd.toFixed(2)) ghostUntil.spendUsd = now + GHOST_MS;
    if (t.agentsTotal !== prevTotals.agentsTotal) ghostUntil.agentsTotal = now + GHOST_MS;
    if (t.tasksInFlight !== prevTotals.tasksInFlight || t.gatesWaiting !== prevTotals.gatesWaiting) {
      ghostUntil.tasksGates = now + GHOST_MS;
    }
  }
  prevTotals = t;
}

function ghostFracFor(until: number, now: number): { active: boolean; frac: number } {
  const active = until > now;
  return { active, frac: active ? 1 - (until - now) / GHOST_MS : 0 };
}

function fmtBig(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return String(Math.round(n));
}

function fmtUsd(n: number): string {
  if (n >= 1000) return "$" + (n / 1000).toFixed(2) + "K";
  return "$" + n.toFixed(2);
}

function recentMax(hist: { t: number; v: number }[]): number {
  let m = 1;
  for (const p of hist) if (p.v > m) m = p.v;
  return m;
}

function drawMeter(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, frac: number, color: string): void {
  const h = 5;
  ctx.fillStyle = "rgba(255,255,255,0.08)";
  ctx.fillRect(x, y, w, h);
  ctx.fillStyle = color;
  ctx.fillRect(x, y, Math.max(0, Math.min(1, frac)) * w, h);
}

/** Round 3 item 5 (HUD meter honesty), round 4 item 2a (SHARED scale):
 * `logMeterFrac`/`LOG_METER_PIPS` now live in scale.ts so cards.ts's
 * per-node throughput bar can point at the exact same fixed anchors --
 * see scale.ts's doc comment for the full "second offense" history.
 * LOG_MIN..LOG_MAX are FIXED (never recomputed from history), so length
 * is monotonic with the number, always: 10 always maps to the same x,
 * 100 always further right than 10, 10k always maps to the same x
 * regardless of what this session's peak happens to be. */
function drawLogMeter(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, value: number, color: string): void {
  const h = 5;
  ctx.fillStyle = "rgba(255,255,255,0.08)";
  ctx.fillRect(x, y, w, h);
  ctx.fillStyle = color;
  ctx.fillRect(x, y, logMeterFrac(value) * w, h);
  ctx.fillStyle = "rgba(18,22,32,0.85)";
  for (const pip of LOG_METER_PIPS) {
    const px = x + logMeterPipFrac(pip) * w;
    ctx.fillRect(px - 0.5, y - 1, 1, h + 2);
  }
}

/** Segmented block meter for a small integer count (agents, tasks in
 * flight, gates waiting) — the grammar's "stacked hollow squares"
 * language, but FILLED per active unit so it doubles as a mini bar chart
 * rather than a bare number.
 *
 * Round 4 item 1b: an optional `liveN` splits the filled segments into
 * two shades -- BRIGHT for units currently producing (0..liveN) and a
 * DIMMER-but-still-visibly-present tone for units that exist but are
 * momentarily quiet (liveN..count). This is what lets the "agents" row
 * show its count as a stable, never-zeroing cumulative total while still
 * being honest that not all of them are transmitting RIGHT NOW, without
 * needing a second number or row. */
function drawSegments(
  ctx: CanvasRenderingContext2D, x: number, y: number, w: number,
  count: number, cap: number, color: string, liveN?: number,
): void {
  const n = Math.min(cap, Math.max(0, count));
  const live = liveN === undefined ? n : Math.min(n, Math.max(0, liveN));
  const gap = 3;
  const segW = (w - gap * (cap - 1)) / cap;
  for (let i = 0; i < cap; i++) {
    const sx = x + i * (segW + gap);
    let fill = "rgba(255,255,255,0.08)";
    if (i < live) fill = color;
    else if (i < n) fill = "rgba(45,212,191,0.32)"; // present but quiet right now
    ctx.fillStyle = fill;
    ctx.fillRect(sx, y, Math.max(1, segW), 5);
  }
}

function statRow(
  ctx: CanvasRenderingContext2D, x: number, y: number, w: number,
  icon: string, valueText: string, valueColor: string,
  ghost: { active: boolean; frac: number }, meter: () => void,
): void {
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillStyle = PALETTE.textLabel;
  ctx.font = "13px system-ui, sans-serif";
  ctx.fillText(icon, x, y);
  drawGhostable(
    ctx, valueText, x + 24, y, valueColor,
    "600 15px ui-monospace, SFMono-Regular, monospace", ghost.active, ghost.frac,
  );
  meter();
  void w;
}

export function drawHud(ctx: CanvasRenderingContext2D, state: GraphState, now: number): void {
  const totals = computeHudTotals(state);
  updateGhosts(totals, now);

  const x = PAD;
  const top = PAD;
  const panelH = HERO_H + STAT_H * 2 + SPARK_H + 22;

  ctx.fillStyle = "rgba(18,22,32,0.8)";
  ctx.beginPath();
  ctx.roundRect(x - 10, top - 10, PANEL_W, panelH, 10);
  ctx.fill();

  const meterW = PANEL_W - 24 - 24;

  // Hero row: round 4 item 1b -- the BIG number is now the cumulative
  // tokens total (never zeroes just because flow paused), with the
  // live RATE as a small dim sub-label ("N/s across live sessions" can
  // honestly read "0/s" while quiet without reading as a crash, because
  // it's clearly labeled a rate sitting next to a total that hasn't
  // moved). The log meter still gauges the RATE (totals.tokS) on the
  // shared fixed scale -- a bar honestly emptying while the headline
  // number holds still is the correct "paused, not dead" read.
  let rowY = top + 6;
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillStyle = PALETTE.textLabel;
  ctx.font = "13px system-ui, sans-serif";
  ctx.fillText("◈", x, rowY);
  const heroGhost = ghostFracFor(ghostUntil.tokensTotal, now);
  drawGhostable(
    ctx, `${fmtBig(totals.tokensTotal)}`, x + 24, rowY, PALETTE.teal,
    "700 26px ui-monospace, SFMono-Regular, monospace", heroGhost.active, heroGhost.frac,
  );
  ctx.fillStyle = PALETTE.textDim;
  ctx.font = "12px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText(`tokens total  ·  ${fmtBig(totals.tokS)}/s now`, x + 24, rowY + 20);
  drawLogMeter(ctx, x, rowY + 30, meterW, totals.tokS, PALETTE.teal);
  rowY += HERO_H;

  // $ spend — meter vs its own recent-max history.
  statRow(
    ctx, x, rowY, meterW, "$", fmtUsd(totals.spendUsd), PALETTE.green,
    ghostFracFor(ghostUntil.spendUsd, now),
    () => drawMeter(ctx, x, rowY + 12, meterW, totals.spendUsd / recentMax(state.spendHistory), PALETTE.green),
  );
  rowY += STAT_H;

  // Agents — round 4 item 1b: the printed count is agentsTotal (every
  // session-kind node present, cumulative -- never zeroes just because
  // nobody's mid-tick right now), the segmented meter shades the
  // currently-transmitting subset bright and the rest dim-but-present,
  // so "how many agents exist" and "how many are producing this instant"
  // both read at a glance without a second number.
  statRow(
    ctx, x, rowY, meterW, "●", `${totals.agentsTotal}`, PALETTE.textPrimary,
    ghostFracFor(ghostUntil.agentsTotal, now),
    () => drawSegments(ctx, x, rowY + 12, meterW, totals.agentsTotal, 12, PALETTE.teal, totals.agentsLiveNow),
  );
  rowY += STAT_H;

  // Tasks in flight / gates waiting — design directive's combined row:
  // "tasks in flight / gates waiting (magenta count when >0)". Normally
  // shows tasksInFlight (cumulative -- never zeroes from mere silence,
  // item 1b's exact ask), and switches to the magenta gate count + label
  // the moment ANY gate is pending, the ONLY row that ever reads as an
  // alarm (locked palette: magenta = "needs a decision").
  const gateAlarm = totals.gatesWaiting > 0;
  statRow(
    ctx, x, rowY, meterW,
    gateAlarm ? "◆" : "▣",
    gateAlarm ? `${totals.gatesWaiting}` : `${totals.tasksInFlight}`,
    gateAlarm ? PALETTE.magenta : PALETTE.textPrimary,
    ghostFracFor(ghostUntil.tasksGates, now),
    () => drawSegments(
      ctx, x, rowY + 12, meterW,
      gateAlarm ? totals.gatesWaiting : totals.tasksInFlight, gateAlarm ? 8 : 12,
      gateAlarm ? PALETTE.magenta : "rgba(154,166,189,0.7)",
    ),
  );
  ctx.fillStyle = PALETTE.textDim;
  ctx.font = "10px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.fillText(gateAlarm ? "gates waiting" : "tasks in flight", x + meterW, rowY);
  rowY += STAT_H - 4;

  // Sparkline: aggregate tok/s trend, taller with a filled area under
  // the line and an emphasized endpoint dot (round1 critic praised the
  // old thin-line sparkline as the dashboard's "one clear, uncontested
  // win" — keep it, make it read better at a glance/downscale).
  const spX = x, spY = rowY, spW = PANEL_W - 24, spH = SPARK_H - 8;
  const hist = state.tokSHistory;
  if (hist.length >= 2) {
    const maxV = Math.max(1, ...hist.map((p) => p.v));
    const minT = hist[0].t, maxT = hist[hist.length - 1].t || minT + 1;
    const pt = (p: { t: number; v: number }) => ({
      x: spX + ((p.t - minT) / Math.max(1, maxT - minT)) * spW,
      y: spY + spH - (p.v / maxV) * spH,
    });
    ctx.beginPath();
    ctx.moveTo(spX, spY + spH);
    hist.forEach((p) => { const q = pt(p); ctx.lineTo(q.x, q.y); });
    ctx.lineTo(spX + spW, spY + spH);
    ctx.closePath();
    ctx.fillStyle = "rgba(45,212,191,0.18)";
    ctx.fill();

    ctx.beginPath();
    hist.forEach((p, i) => { const q = pt(p); if (i === 0) ctx.moveTo(q.x, q.y); else ctx.lineTo(q.x, q.y); });
    ctx.strokeStyle = PALETTE.teal;
    ctx.lineWidth = 1.6;
    ctx.globalAlpha = 0.95;
    ctx.stroke();
    ctx.globalAlpha = 1;

    const last = pt(hist[hist.length - 1]);
    ctx.beginPath();
    ctx.arc(last.x, last.y, 3, 0, Math.PI * 2);
    ctx.fillStyle = PALETTE.teal;
    ctx.fill();
  } else {
    ctx.beginPath();
    ctx.moveTo(spX, spY + spH);
    ctx.lineTo(spX + spW, spY + spH);
    ctx.strokeStyle = PALETTE.teal;
    ctx.lineWidth = 1.4;
    ctx.globalAlpha = 0.9;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }
}

/** Round 3 item 2: a small fixed legend chip, bottom-left, screen space
 * -- names the node-kind/role glyph vocabulary (item 2's icons) and the
 * 5 locked state colors (item 6's legend text) in one glance, so a
 * viewer never has to already know the convention to read the canvas.
 * Kept deliberately tiny/low-contrast itself (it's a REFERENCE, not
 * content) -- never competes with the cards/wires it's explaining. */
const LEGEND_GLYPHS: { glyph: string; label: string }[] = [
  { glyph: glyphFor("task"), label: "task" },
  { glyph: glyphFor("subtask"), label: "subtask" },
  { glyph: glyphFor("session", "dev"), label: "dev" },
  { glyph: glyphFor("session", "qa"), label: "qa" },
  { glyph: glyphFor("session", "sm"), label: "sm" },
];

const LEGEND_STATES: { color: string; label: string }[] = [
  { color: PALETTE.teal, label: "working" },
  { color: PALETTE.magenta, label: "waiting" },
  { color: PALETTE.textDim, label: "not started" },
  { color: PALETTE.red, label: "dead" },
  { color: PALETTE.green, label: "done" },
];

export function drawLegend(ctx: CanvasRenderingContext2D, x: number, bottomY: number): void {
  const rowH = 16;
  const panelH = rowH * 2 + 16;
  const panelW = 372;
  const top = bottomY - panelH;

  ctx.save();
  ctx.fillStyle = "rgba(18,22,32,0.72)";
  ctx.beginPath();
  ctx.roundRect(x - 8, top, panelW, panelH, 8);
  ctx.fill();

  ctx.textBaseline = "middle";
  ctx.font = "10px system-ui, sans-serif";

  // Glyph row.
  let gx = x;
  const gy = top + 12;
  for (const g of LEGEND_GLYPHS) {
    ctx.fillStyle = PALETTE.textLabel;
    ctx.textAlign = "left";
    ctx.fillText(g.glyph, gx, gy);
    ctx.fillStyle = PALETTE.textDim;
    ctx.fillText(g.label, gx + 14, gy);
    gx += 14 + g.label.length * 5.6 + 12;
  }

  // Color-swatch row.
  let sx = x;
  const sy = top + 12 + rowH;
  for (const s of LEGEND_STATES) {
    ctx.beginPath();
    ctx.arc(sx + 4, sy, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = s.color;
    ctx.fill();
    ctx.fillStyle = PALETTE.textDim;
    ctx.textAlign = "left";
    ctx.fillText(s.label, sx + 12, sy);
    sx += 12 + s.label.length * 5.6 + 12;
  }
  ctx.restore();
}
