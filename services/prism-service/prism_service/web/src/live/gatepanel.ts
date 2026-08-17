/** Round 3 item 7: a PERSISTENT docked right-edge panel listing every
 * node currently waiting on a gate -- the grammar's "boss-bar/breach-
 * panel" pattern (VISUAL_GRAMMAR.md §2's "Corporation" threat panel,
 * always docked to the same screen edge so a player never has to hunt
 * for it). Round1/2 funneled EVERY node-level event (both a completion
 * AND a gate-wait) through one generic bottom-right toast, differing
 * only by a thin border tint and text -- critic 3's exact complaint
 * ("every node-level event funnels through one generic bottom-right
 * toast"). A gate wait is not a momentary notification, it's an ONGOING
 * STATE (a task can sit gated for many minutes), so it now gets its own
 * always-visible, never-fading panel computed fresh from live node state
 * every frame -- never a one-shot spawned event like toasts.ts's
 * completion toasts, which correctly stay momentary because a
 * completion actually IS a one-shot moment. */

import type { LiveNode } from "./graphState";
import { PALETTE } from "./palette";

const PANEL_W = 232;
const ROW_H = 34;
const HEADER_H = 26;
const MARGIN = 16;

function fmtWaitingMins(waitS: number): string {
  const m = Math.floor(waitS / 60);
  return m < 1 ? "<1m" : `${m}m`;
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** Task edf38154 (owner 2026-08-17, "this says im waiting on gates but im
 * not, there is nothing i can do for them"): a pending gate is no longer
 * one undifferentiated alarm list. Partitioned on n.owner_actionable --
 * derived server-side by api/work.py's _gate_actionability, which itself
 * only ever CALLS api/conductor.py's gate_readiness (never a second,
 * drifting implementation of its teeth, mx-d6c1df) -- into a magenta
 * "YOUR REVIEW" section (things only the owner can clear) and a dim
 * "waiting on others" section (fully listed, never capped, each row
 * naming who/what it's actually waiting on via n.waiting_on) so the two
 * read as genuinely different, not the same card in a different color. */
export function drawGatePanel(
  ctx: CanvasRenderingContext2D, nodes: LiveNode[], now: number, screenW: number, screenH: number,
): void {
  const pending = nodes.filter((n) => n.gate_state === "pending");
  if (!pending.length) return;
  const mine = pending.filter((n) => n.owner_actionable);
  const others = pending.filter((n) => !n.owner_actionable);

  const sectionH = (rows: number) => (rows ? HEADER_H + rows * ROW_H : 0);
  const panelH = 8 + sectionH(mine.length) + sectionH(others.length);
  const x = screenW - MARGIN - PANEL_W;
  const top = Math.max(MARGIN, (screenH - panelH) / 2);

  ctx.save();
  ctx.beginPath();
  ctx.roundRect(x, top, PANEL_W, panelH, 8);
  ctx.fillStyle = "rgba(18,22,32,0.92)";
  ctx.fill();
  ctx.strokeStyle = PALETTE.magenta;
  ctx.lineWidth = 1.4;
  ctx.stroke();
  // Left accent stripe -- same "docked, magenta, unmissable" language a
  // waiting-gate card itself uses (cards.ts's EDGE_STRIPE_W).
  ctx.beginPath();
  ctx.roundRect(x, top, 4, panelH, [8, 0, 0, 8]);
  ctx.fillStyle = PALETTE.magenta;
  ctx.fill();

  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  let rowY = top;

  if (mine.length) {
    ctx.fillStyle = PALETTE.magenta;
    ctx.font = "600 11px system-ui, sans-serif";
    ctx.fillText(`◆ YOUR REVIEW (${mine.length})`, x + 14, rowY + HEADER_H / 2 + 2);
    rowY += HEADER_H;
    for (const n of mine) {
      const waitS = (now - n.gatePendingSince) / 1000;
      ctx.fillStyle = PALETTE.textPrimary;
      ctx.font = "600 11px system-ui, sans-serif";
      ctx.fillText(truncate(n.label, 26), x + 14, rowY + 12);
      ctx.fillStyle = PALETTE.magenta;
      ctx.font = "10px system-ui, sans-serif";
      ctx.fillText(`${n.workflow_step || "gate"} · waiting ${fmtWaitingMins(waitS)}`, x + 14, rowY + 25);
      rowY += ROW_H;
    }
  }

  // "waiting on others" -- FULL, uncapped list (never sliced/truncated to
  // N rows): a driver-owned or machine-seat gate is still real state the
  // owner is entitled to see, it's just dim because it's not theirs to
  // click.
  if (others.length) {
    ctx.fillStyle = PALETTE.textDim;
    ctx.font = "600 11px system-ui, sans-serif";
    ctx.fillText(`waiting on others (${others.length})`, x + 14, rowY + HEADER_H / 2 + 2);
    rowY += HEADER_H;
    for (const n of others) {
      ctx.fillStyle = PALETTE.textDim;
      ctx.font = "600 11px system-ui, sans-serif";
      ctx.fillText(truncate(n.label, 26), x + 14, rowY + 12);
      ctx.font = "10px system-ui, sans-serif";
      ctx.fillText(n.waiting_on || n.workflow_step || "gate", x + 14, rowY + 25);
      rowY += ROW_H;
    }
  }
  ctx.restore();
}
