/** Node card rendering — opaque slate card, brighter title bar, small
 * leading glyph, NO border unless selected (design directive). Stat rows
 * carry a connector dot (filled+saturated when flowing, hollow RED ring
 * when the signal is dead) plus a value that GHOSTS (double-exposes)
 * for a beat whenever it just changed — the primary "alive" cue. */

import type { LiveNode } from "./graphState";
import { PALETTE, glyphFor } from "./palette";

const TITLE_H = 24;
const PAD_X = 10;
const ROW_H = 19;
const ROW_START_Y = TITLE_H + 9;
const DOT_R = 3.5;

function truncate(s: string, n: number): string {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return String(Math.round(n));
}

/** A value row just changed within GHOST_WINDOW of `now` -> draw it twice,
 * a soft second copy offset + faded, to read as a double-exposed tween
 * even though we don't have a real intermediate value to animate through
 * (grammar §1/§7: "the number briefly renders in two overlapping states"). */
/** Exported so hud.ts (round 2, piece 3) reuses the exact same
 * double-exposed-tween technique for its own big numbers, per the build
 * directive: "Numbers ghost/tween when they change (reuse cards.ts's
 * ghosting approach)" — one visual language for "this value just
 * moved", not two. */
export function drawGhostable(
  ctx: CanvasRenderingContext2D, text: string, x: number, y: number,
  color: string, font: string, ghostActive: boolean, ghostFrac: number,
): void {
  ctx.font = font;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  if (ghostActive) {
    ctx.globalAlpha = 0.35 * (1 - ghostFrac);
    ctx.fillStyle = color;
    ctx.fillText(text, x - 1, y - 2 - ghostFrac * 3);
    ctx.globalAlpha = 1;
  }
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

function drawConnectorDot(ctx: CanvasRenderingContext2D, x: number, y: number, color: string, live: boolean): void {
  ctx.beginPath();
  ctx.arc(x, y, DOT_R, 0, Math.PI * 2);
  if (live) {
    ctx.fillStyle = color;
    ctx.fill();
  } else {
    ctx.strokeStyle = PALETTE.red;
    ctx.lineWidth = 1.4;
    ctx.stroke();
  }
}

function drawCapacityBar(
  ctx: CanvasRenderingContext2D, x: number, y: number, w: number, frac: number,
  color: string, live: boolean, h = 4,
): void {
  ctx.fillStyle = "rgba(255,255,255,0.08)";
  ctx.fillRect(x, y, w, h);
  if (live) {
    ctx.fillStyle = color;
    ctx.fillRect(x, y, Math.max(0, Math.min(1, frac)) * w, h);
  }
}

export type CardMetrics = {
  tokS: number | null;
  tokensTotal: number | null;
  tokensLive: boolean;
  step: string;
  stepBarFrac: number;
  stepLive: boolean;
  gatePending: boolean;
  gateLabel: string;
  queueDepth: number;
  /** Recent-throughput buffer bar fraction (0..1) — task/subtask cards
   * only (round 2, piece 5: "load as LENGTH"). */
  bufferFrac: number;
};

const STATUS_ICON: Record<string, string> = {
  done: "✓",
  in_progress: "",
  pending: "",
};

export function drawCard(
  ctx: CanvasRenderingContext2D, n: LiveNode, m: CardMetrics, now: number,
): void {
  const { x, y } = n;
  const { w, h } = n.slot;
  const r = 6;

  // Card body — opaque slate, no border unless selected.
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
  ctx.fillStyle = PALETTE.card;
  ctx.fill();

  // Heartbeat pulse: expanding, fading ring around the whole card.
  if (n.pulseUntil > now) {
    const remaining = n.pulseUntil - now;
    const frac = 1 - remaining / 700;
    ctx.beginPath();
    ctx.roundRect(x - frac * 5, y - frac * 5, w + frac * 10, h + frac * 10, r + frac * 5);
    ctx.strokeStyle = PALETTE.orange;
    ctx.globalAlpha = Math.max(0, 0.6 - frac * 0.6);
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  if (n.selected) {
    ctx.beginPath();
    ctx.roundRect(x + 1, y + 1, w - 2, h - 2, r);
    ctx.strokeStyle = PALETTE.selection;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // Title bar.
  const doneish = n.status === "done" || n.gate_state === "passed";
  ctx.save();
  ctx.beginPath();
  ctx.roundRect(x, y, w, TITLE_H, [r, r, 0, 0]);
  ctx.clip();
  ctx.fillStyle = doneish ? PALETTE.cardTitleDone : PALETTE.cardTitle;
  ctx.fillRect(x, y, w, TITLE_H);
  ctx.restore();

  ctx.fillStyle = PALETTE.textLabel;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(glyphFor(n.kind), x + PAD_X, y + TITLE_H / 2);

  ctx.fillStyle = PALETTE.textPrimary;
  ctx.font = "600 12px system-ui, sans-serif";
  const titleMaxChars = Math.max(8, Math.floor((w - 44) / 6.4));
  ctx.fillText(truncate(n.label, titleMaxChars), x + PAD_X + 16, y + TITLE_H / 2);

  const icon = STATUS_ICON[n.status] ?? "";
  if (icon) {
    ctx.fillStyle = PALETTE.green;
    ctx.textAlign = "right";
    ctx.fillText(icon, x + w - PAD_X, y + TITLE_H / 2);
  }

  // Rows.
  let rowY = y + ROW_START_Y;
  const dotX = x + PAD_X + DOT_R;
  const labelX = dotX + 10;
  const valueRight = x + w - PAD_X;

  // Tokens row (teal).
  {
    const ghostActive = n.tokensGhostUntil > now;
    const ghostFrac = ghostActive ? 1 - (n.tokensGhostUntil - now) / 650 : 0;
    drawConnectorDot(ctx, dotX, rowY, PALETTE.teal, m.tokensLive);
    ctx.fillStyle = PALETTE.textLabel;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText("Tokens", labelX, rowY);
    const rateSuffix = m.tokensLive && m.tokS ? ` [${fmtNum(m.tokS)}/s]` : "";
    const valueText = `${fmtNum(m.tokensTotal)}${rateSuffix}`;
    ctx.textAlign = "right";
    ctx.font = "11px ui-monospace, SFMono-Regular, monospace";
    const tw = ctx.measureText(valueText).width;
    drawGhostable(ctx, valueText, valueRight - tw, rowY, m.tokensLive ? PALETTE.teal : PALETTE.textDim, "11px ui-monospace, SFMono-Regular, monospace", ghostActive, ghostFrac);
    rowY += ROW_H;
  }

  // Recent-throughput BUFFER bar (round 2, piece 5) — task/subtask cards
  // only, ~70% of card width, teal fill on a dark track. Fills a real
  // amount on every tokens.turn that targets this node and drains
  // continuously in GraphState.step(), so its LENGTH alone answers "how
  // loaded is this right now" (round1 critic: "no node ever shows load
  // as a filling bar... every load judgment forces reading and
  // comparing five-digit numbers instead of glancing at a bar").
  if (n.kind !== "session") {
    const barW = Math.min(w * 0.7, valueRight - labelX);
    rowY += 8;
    drawCapacityBar(ctx, labelX, rowY, barW, m.bufferFrac, PALETTE.teal, m.bufferFrac > 0.015, 6);
    rowY += ROW_H - 8;
  }

  // Step row (orange) + capacity bar — task/subtask cards only. A session
  // card has no workflow_step of its own (it just drives one), so this
  // row would only ever read as a permanently-dead 0; the directive's
  // session card anatomy is "its own tokens row, wire up to its task",
  // not a copy of the task's step.
  if (n.kind !== "session") {
    const ghostActive = n.stepGhostUntil > now;
    const ghostFrac = ghostActive ? 1 - (n.stepGhostUntil - now) / 650 : 0;
    drawConnectorDot(ctx, dotX, rowY, PALETTE.orange, m.stepLive);
    ctx.fillStyle = PALETTE.textLabel;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("Step", labelX, rowY);
    const stepText = m.step ? truncate(m.step, 16) : "0";
    ctx.textAlign = "right";
    ctx.font = "10px system-ui, sans-serif";
    const sw = ctx.measureText(stepText).width;
    drawGhostable(ctx, stepText, valueRight - sw, rowY, m.stepLive ? PALETTE.orange : PALETTE.textDim, "10px system-ui, sans-serif", ghostActive, ghostFrac);
    rowY += 10;
    drawCapacityBar(ctx, labelX, rowY, valueRight - labelX, m.stepBarFrac, PALETTE.orange, m.stepLive);
    rowY += ROW_H - 8;
  }

  // Queue depth: stacked hollow squares beside the step row when a task
  // has subtasks waiting (grammar §2: "backed up / queued").
  if (m.queueDepth > 0) {
    ctx.strokeStyle = PALETTE.textDim;
    ctx.lineWidth = 1;
    for (let i = 0; i < Math.min(m.queueDepth, 4); i++) {
      ctx.strokeRect(labelX + i * 8, rowY - 12, 6, 6);
    }
  }

  // Spend row (green) — not wired to a real $ source yet (see task
  // report's "known weaknesses"); renders honestly dead per the
  // directive's own rule (absent signal -> hollow red ring, flat 0)
  // rather than fabricating a number.
  if (h >= 96) {
    drawConnectorDot(ctx, dotX, rowY, PALETTE.green, false);
    ctx.fillStyle = PALETTE.textLabel;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("Spend", labelX, rowY);
    ctx.fillStyle = PALETTE.textDim;
    ctx.textAlign = "right";
    ctx.font = "11px ui-monospace, SFMono-Regular, monospace";
    ctx.fillText("$0", valueRight, rowY);
    rowY += ROW_H;
  }

  // Gate row (magenta) — only when a gate is actually pending.
  if (m.gatePending && rowY + 4 < y + h) {
    ctx.beginPath();
    ctx.arc(dotX, rowY, DOT_R, 0, Math.PI * 2);
    ctx.fillStyle = PALETTE.magenta;
    ctx.fill();
    ctx.fillStyle = PALETTE.magenta;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(truncate(`gate: ${m.gateLabel}`, 24), labelX, rowY);
  }
}
