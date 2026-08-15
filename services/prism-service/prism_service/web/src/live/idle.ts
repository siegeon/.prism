/** Loading and quiet states. Per the directive there is NO full-screen
 * "nothing is happening" dead state once booted — quiet means the HUD
 * stays up with zeroed rates, the grid stays, completed cards stay
 * dimly parked, and a single "queue is quiet" line appears; a locally
 * dead node (no driver) is a hollow red ring on ITS rows, handled by
 * cards.ts, not here. The only bare-grid state this module owns is
 * pre-boot loading, matching the reference game's "Labs OS / Initializing"
 * screen (VISUAL_GRAMMAR.md §6). */

import { PALETTE } from "./palette";

export function drawLoading(ctx: CanvasRenderingContext2D, w: number, h: number, now: number, version: string): void {
  ctx.fillStyle = PALETTE.textLabel;
  ctx.font = "13px ui-monospace, SFMono-Regular, monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const dots = ".".repeat(1 + Math.floor((now / 400) % 3));
  ctx.fillText(`PRISM OS ${version} / initializing${dots}`, w / 2, h / 2);

  const spin = (now / 260) % (Math.PI * 2);
  ctx.beginPath();
  ctx.arc(w / 2, h / 2 - 26, 8, spin, spin + Math.PI * 1.4);
  ctx.strokeStyle = PALETTE.orange;
  ctx.lineWidth = 2;
  ctx.stroke();
}

/** Drawn in-canvas, screen space, near the HUD — never a full-screen
 * overlay — when nothing has moved (no live tok/s, no packets, no
 * recent pulse) across the whole graph. */
export function drawQuietLine(ctx: CanvasRenderingContext2D, x: number, y: number): void {
  ctx.fillStyle = PALETTE.textDim;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText("queue is quiet", x, y);
}

export function isGraphQuiet(state: { nodes: { tok_s: number | null; pulseUntil: number }[]; packets: unknown[] }, now: number): boolean {
  if (state.packets.length > 0) return false;
  for (const n of state.nodes) {
    if (n.tok_s && n.tok_s > 0) return false;
    if (n.pulseUntil > now) return false;
  }
  return true;
}
