/** In-transit packet markers: small filled squares riding a wire from
 * source to target, spawned on real tokens.turn events only (design
 * directive: "every piece of motion driven by REAL events"; grammar §1:
 * SPARSE density, "one visible marker per 300-600px", legibility over
 * spectacle). Round 2 (piece 2 critic fix): SPEED is now CONSTANT
 * real-world px/s regardless of tok_s ("constant modest speed... a
 * marker is visibly mid-wire in any 2s window") -- it's SPAWN FREQUENCY
 * (gated in graphState.ts by a per-wire cooldown) that scales with
 * tok_s, never the travel speed. Kept as a tiny, dependency-free module
 * so it's easy for a later lane to retune density/speed without
 * touching layout or draw. */

import type { Point } from "./wires";
import { pointAtFraction, polylineLength } from "./wires";
import { PALETTE } from "./palette";

export type Packet = {
  edgeKey: string;
  pts: Point[];
  t: number;
  fracPerMs: number;
};

/** ~140px/s -- fast enough to read as motion, slow enough that a marker
 * sits visibly mid-span for a full second or more on a typical wire
 * (round1 critic: markers must be seen "sitting partway along at least
 * one edge, not only at the nodes themselves"). */
const PX_PER_MS = 140 / 1000;

export function spawnPacket(edgeKey: string, pts: Point[]): Packet {
  const len = Math.max(1, polylineLength(pts));
  return { edgeKey, pts, t: 0, fracPerMs: PX_PER_MS / len };
}

export function stepPackets(packets: Packet[], dtMs: number): Packet[] {
  if (!packets.length) return packets;
  for (const p of packets) p.t += p.fracPerMs * dtMs;
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
