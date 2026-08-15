/** Fixed HUD overlay, top-left, independent of pan/zoom (design
 * directive). Basic version for piece 1: tok/s across live sessions,
 * agents live, tasks in flight / gates waiting, one sparkline of
 * aggregate tok/s. $ spend is intentionally omitted rather than
 * fabricated — nothing in the graph wires a real dollar figure yet
 * (see cards.ts's Spend row comment); a later lane adds it once spend
 * is plumbed rather than faking a number here. */

import type { GraphState } from "./graphState";
import { PALETTE } from "./palette";

const ROW_H = 26;
const PAD = 14;

export type HudTotals = {
  tokS: number;
  agentsLive: number;
  tasksInFlight: number;
  gatesWaiting: number;
};

export function computeHudTotals(state: GraphState): HudTotals {
  let tokS = 0;
  let agentsLive = 0;
  let tasksInFlight = 0;
  let gatesWaiting = 0;
  for (const n of state.nodes) {
    if (n.kind === "session") {
      if (n.tok_s && n.tok_s > 0) agentsLive += 1;
      tokS += n.tok_s || 0;
    } else {
      if (n.status === "in_progress") tasksInFlight += 1;
      if (n.gate_state === "pending") gatesWaiting += 1;
    }
  }
  return { tokS, agentsLive, tasksInFlight, gatesWaiting };
}

function row(ctx: CanvasRenderingContext2D, x: number, y: number, icon: string, big: string, sub: string, subColor: string): void {
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillStyle = PALETTE.textLabel;
  ctx.font = "13px system-ui, sans-serif";
  ctx.fillText(icon, x, y);
  ctx.fillStyle = PALETTE.textPrimary;
  ctx.font = "600 15px ui-monospace, SFMono-Regular, monospace";
  ctx.fillText(big, x + 22, y);
  if (sub) {
    const bw = ctx.measureText(big).width;
    ctx.fillStyle = subColor;
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillText(sub, x + 22 + bw + 8, y);
  }
}

export function drawHud(ctx: CanvasRenderingContext2D, state: GraphState, now: number): void {
  const totals = computeHudTotals(state);
  const x = PAD, top = PAD;
  const panelW = 210, panelH = ROW_H * 4 + 40;

  ctx.fillStyle = "rgba(18,22,32,0.72)";
  ctx.beginPath();
  ctx.roundRect(x - 8, top - 8, panelW, panelH, 8);
  ctx.fill();

  row(ctx, x, top + ROW_H * 0 + 8, "◈", `${Math.round(totals.tokS)}`, "tok/s", PALETTE.teal);
  row(ctx, x, top + ROW_H * 1 + 8, "▣", `${state.nodes.filter((n) => n.kind === "task").length}`, "root tasks", PALETTE.textDim);
  row(ctx, x, top + ROW_H * 2 + 8, "●", `${totals.agentsLive}`, "agents live", PALETTE.textDim);
  row(
    ctx, x, top + ROW_H * 3 + 8, "◆",
    `${totals.tasksInFlight}`,
    totals.gatesWaiting > 0 ? `${totals.gatesWaiting} gate${totals.gatesWaiting === 1 ? "" : "s"} waiting` : "in flight",
    totals.gatesWaiting > 0 ? PALETTE.magenta : PALETTE.textDim,
  );

  // Sparkline: aggregate tok/s over the last ~5 minutes.
  const spX = x, spY = top + ROW_H * 4 + 6, spW = panelW - 24, spH = 22;
  const hist = state.tokSHistory;
  ctx.strokeStyle = PALETTE.teal;
  ctx.lineWidth = 1.4;
  ctx.globalAlpha = 0.9;
  if (hist.length >= 2) {
    const maxV = Math.max(1, ...hist.map((p) => p.v));
    const minT = hist[0].t, maxT = hist[hist.length - 1].t || minT + 1;
    ctx.beginPath();
    hist.forEach((p, i) => {
      const px = spX + ((p.t - minT) / Math.max(1, maxT - minT)) * spW;
      const py = spY + spH - (p.v / maxV) * spH;
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.stroke();
  } else {
    ctx.beginPath();
    ctx.moveTo(spX, spY + spH);
    ctx.lineTo(spX + spW, spY + spH);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
  void now;
}
