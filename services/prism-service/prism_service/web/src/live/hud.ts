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
import { PALETTE } from "./palette";
import { drawGhostable } from "./cards";

const PAD = 14;
const PANEL_W = 320;
const HERO_H = 54;
const STAT_H = 40;
const SPARK_H = 46;
const GHOST_MS = 650;

export type HudTotals = {
  tokS: number;
  spendUsd: number;
  agentsLive: number;
  gatesWaiting: number;
};

export function computeHudTotals(state: GraphState): HudTotals {
  let tokS = 0;
  let spendUsd = 0;
  let agentsLive = 0;
  let gatesWaiting = 0;
  for (const n of state.nodes) {
    if (n.kind === "session") {
      if (n.tok_s && n.tok_s > 0) agentsLive += 1;
      tokS += n.tok_s || 0;
    } else {
      if (n.gate_state === "pending") gatesWaiting += 1;
      spendUsd += n.spend_usd || 0;
    }
  }
  return { tokS, spendUsd, agentsLive, gatesWaiting };
}

// Module-scope ghost bookkeeping: the HUD's numbers are derived
// aggregates with no natural "node" to hang a ghostUntil timestamp on
// (unlike cards.ts), so a small closure-local record does the same job.
let prevTotals: HudTotals | null = null;
const ghostUntil = { tokS: 0, spendUsd: 0, agentsLive: 0, gatesWaiting: 0 };

function updateGhosts(t: HudTotals, now: number): void {
  if (prevTotals) {
    if (Math.round(t.tokS) !== Math.round(prevTotals.tokS)) ghostUntil.tokS = now + GHOST_MS;
    if (t.spendUsd.toFixed(2) !== prevTotals.spendUsd.toFixed(2)) ghostUntil.spendUsd = now + GHOST_MS;
    if (t.agentsLive !== prevTotals.agentsLive) ghostUntil.agentsLive = now + GHOST_MS;
    if (t.gatesWaiting !== prevTotals.gatesWaiting) ghostUntil.gatesWaiting = now + GHOST_MS;
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

/** Segmented block meter for a small integer count (agents live, gates
 * waiting) — the grammar's "stacked hollow squares" language, but FILLED
 * per active unit so it doubles as a mini bar chart rather than a bare
 * number. */
function drawSegments(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, count: number, cap: number, color: string): void {
  const n = Math.min(cap, Math.max(0, count));
  const gap = 3;
  const segW = (w - gap * (cap - 1)) / cap;
  for (let i = 0; i < cap; i++) {
    const sx = x + i * (segW + gap);
    ctx.fillStyle = i < n ? color : "rgba(255,255,255,0.08)";
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

  // Hero row: tok/s, bigger than every other value on the panel.
  let rowY = top + 6;
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillStyle = PALETTE.textLabel;
  ctx.font = "13px system-ui, sans-serif";
  ctx.fillText("◈", x, rowY);
  const heroGhost = ghostFracFor(ghostUntil.tokS, now);
  drawGhostable(
    ctx, `${fmtBig(totals.tokS)}`, x + 24, rowY, PALETTE.teal,
    "700 26px ui-monospace, SFMono-Regular, monospace", heroGhost.active, heroGhost.frac,
  );
  ctx.fillStyle = PALETTE.textDim;
  ctx.font = "12px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText("tok/s across live sessions", x + 24, rowY + 20);
  drawMeter(ctx, x, rowY + 30, meterW, totals.tokS / recentMax(state.tokSHistory), PALETTE.teal);
  rowY += HERO_H;

  // $ spend — meter vs its own recent-max history.
  statRow(
    ctx, x, rowY, meterW, "$", fmtUsd(totals.spendUsd), PALETTE.green,
    ghostFracFor(ghostUntil.spendUsd, now),
    () => drawMeter(ctx, x, rowY + 12, meterW, totals.spendUsd / recentMax(state.spendHistory), PALETTE.green),
  );
  rowY += STAT_H;

  // Agents live — segmented teal blocks.
  statRow(
    ctx, x, rowY, meterW, "●", `${totals.agentsLive}`, PALETTE.textPrimary,
    ghostFracFor(ghostUntil.agentsLive, now),
    () => drawSegments(ctx, x, rowY + 12, meterW, totals.agentsLive, 12, PALETTE.teal),
  );
  rowY += STAT_H;

  // Gates waiting — segmented magenta blocks; the ONLY row that ever
  // reads as an alarm (locked palette: magenta = "needs a decision").
  statRow(
    ctx, x, rowY, meterW, "◆", `${totals.gatesWaiting}`,
    totals.gatesWaiting > 0 ? PALETTE.magenta : PALETTE.textPrimary,
    ghostFracFor(ghostUntil.gatesWaiting, now),
    () => drawSegments(ctx, x, rowY + 12, meterW, totals.gatesWaiting, 8, PALETTE.magenta),
  );
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
