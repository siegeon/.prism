/** In-transit packet markers: small filled squares riding a wire from
 * source to target, spawned on real tokens.turn events only (design
 * directive: "every piece of motion driven by REAL events"; grammar §1:
 * SPARSE density, "one visible marker per 300-600px", legibility over
 * spectacle). Kept as a tiny, dependency-free module so it's easy for a
 * later lane to retune density/speed without touching layout or draw. */

import type { Point } from "./wires";
import { pointAtFraction } from "./wires";
import { PALETTE } from "./palette";

export type Packet = {
  edgeKey: string;
  pts: Point[];
  t: number;
  speedPerMs: number;
};

const MIN_TOK_S = 5;
const MAX_TOK_S = 400;
const MIN_SPEED_PER_MS = 1 / 4500; // crosses a wire in ~4.5s (sparse, legible)
const MAX_SPEED_PER_MS = 1 / 900; // ~0.9s for a very hot session

export function speedForTokS(tokS: number): number {
  const clamped = Math.max(MIN_TOK_S, Math.min(MAX_TOK_S, tokS || MIN_TOK_S));
  const frac = (clamped - MIN_TOK_S) / (MAX_TOK_S - MIN_TOK_S);
  return MIN_SPEED_PER_MS + frac * (MAX_SPEED_PER_MS - MIN_SPEED_PER_MS);
}

export function spawnPacket(edgeKey: string, pts: Point[], tokS: number): Packet {
  return { edgeKey, pts, t: 0, speedPerMs: speedForTokS(tokS) };
}

export function stepPackets(packets: Packet[], dtMs: number): Packet[] {
  if (!packets.length) return packets;
  for (const p of packets) p.t += p.speedPerMs * dtMs;
  return packets.filter((p) => p.t < 1);
}

export function drawPackets(ctx: CanvasRenderingContext2D, packets: Packet[]): void {
  ctx.fillStyle = PALETTE.packet;
  for (const p of packets) {
    if (p.pts.length < 2) continue;
    const at = pointAtFraction(p.pts, p.t);
    ctx.beginPath();
    ctx.rect(at.x - 3, at.y - 3, 6, 6);
    ctx.fill();
  }
}
