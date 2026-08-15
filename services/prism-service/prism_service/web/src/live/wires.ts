/** Orthogonal (90-degree) edge routing between card anchors, drawn UNDER
 * cards. One wire = one polyline of 2-4 points, never diagonal, matching
 * the reference grammar's "sharp 90-degree ORTHOGONAL routing, flat 2-3px
 * lines" (VISUAL_GRAMMAR.md §1). Also owns computing a point at fraction
 * t along that polyline, for packets.ts to place in-transit markers on. */

import type { Slot } from "./layout";
import { PALETTE } from "./palette";

export type Point = { x: number; y: number };

/** Elbow connector between the two card edges CLOSEST to each other, so
 * the wire never has to re-enter the card it just left. Picks the
 * dominant axis (whichever separation is larger) and anchors both ends
 * on that axis's facing edges — right->left when the target sits to the
 * right (task -> fanned subtask), top<->bottom when it sits above/below
 * (session card parked under the task it drives). */
export function routeOrthogonal(from: Slot, to: Slot): Point[] {
  const fcx = from.x + from.w / 2, fcy = from.y + from.h / 2;
  const tcx = to.x + to.w / 2, tcy = to.y + to.h / 2;
  const dx = tcx - fcx, dy = tcy - fcy;

  if (Math.abs(dx) >= Math.abs(dy)) {
    const goingRight = dx >= 0;
    const sx = goingRight ? from.x + from.w : from.x;
    const sy = fcy;
    const tx = goingRight ? to.x : to.x + to.w;
    const ty = tcy;
    const midX = sx + (tx - sx) / 2;
    return [{ x: sx, y: sy }, { x: midX, y: sy }, { x: midX, y: ty }, { x: tx, y: ty }];
  }

  const goingDown = dy >= 0;
  const sx = fcx;
  const sy = goingDown ? from.y + from.h : from.y;
  const tx = tcx;
  const ty = goingDown ? to.y : to.y + to.h;
  const midY = sy + (ty - sy) / 2;
  return [{ x: sx, y: sy }, { x: sx, y: midY }, { x: tx, y: midY }, { x: tx, y: ty }];
}

export function polylineLength(pts: Point[]): number {
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    total += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
  }
  return total;
}

/** Point at fraction t (0..1) along a polyline, walking segment lengths. */
export function pointAtFraction(pts: Point[], t: number): Point {
  if (pts.length < 2) return pts[0] ?? { x: 0, y: 0 };
  const total = polylineLength(pts);
  if (total === 0) return pts[0];
  let target = Math.max(0, Math.min(1, t)) * total;
  for (let i = 1; i < pts.length; i++) {
    const segLen = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    if (target <= segLen || i === pts.length - 1) {
      const frac = segLen === 0 ? 0 : target / segLen;
      return {
        x: pts[i - 1].x + (pts[i].x - pts[i - 1].x) * frac,
        y: pts[i - 1].y + (pts[i].y - pts[i - 1].y) * frac,
      };
    }
    target -= segLen;
  }
  return pts[pts.length - 1];
}

export type WireKind = "token" | "structure";

/** `flowing` means "this specific wire has carried real token motion
 * recently" -- for a token (session->task) wire that's live tok_s>0; for
 * a structure (parent_of) wire it's graphState's ~4s recency window on
 * propagated flow (round1 critic: "the structural edges... never show
 * color, a marker, or motion" -- a structure wire now tints faintly teal
 * while flow is propagating up it, dim neutral otherwise, so color still
 * never means two things in the same view: full-saturation teal stays
 * reserved for the token wire itself). */
export function wireColor(kind: WireKind, flowing: boolean): string {
  if (kind === "token") return PALETTE.teal;
  return flowing ? "rgba(45,212,191,0.4)" : "rgba(255,255,255,0.16)";
}

export function drawWire(ctx: CanvasRenderingContext2D, pts: Point[], kind: WireKind, live: boolean): void {
  if (pts.length < 2) return;
  ctx.beginPath();
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
  ctx.strokeStyle = wireColor(kind, live);
  ctx.lineWidth = kind === "token" ? 2.5 : 2;
  ctx.globalAlpha = kind === "token" ? (live ? 0.9 : 0.35) : (live ? 0.75 : 0.5);
  ctx.stroke();
  ctx.globalAlpha = 1;
}
