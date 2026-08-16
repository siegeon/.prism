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

/** Round 3 item 3: bumped from 6x6 to 7x7 with a dark outline stroke so a
 * marker reads as unmistakable against EITHER a bright flowing wire or a
 * dim idle one -- the outline is what actually buys the contrast (a
 * near-white fill alone still washes out against a bright teal wire at
 * video-compression bitrates; the dark ring holds its shape regardless
 * of what's underneath). */
export function drawPackets(ctx: CanvasRenderingContext2D, packets: Packet[]): void {
  for (const p of packets) {
    if (p.pts.length < 2) continue;
    const at = pointAtFraction(p.pts, p.t);
    ctx.beginPath();
    ctx.rect(at.x - 3.5, at.y - 3.5, 7, 7);
    ctx.fillStyle = PALETTE.packet;
    ctx.fill();
    ctx.lineWidth = 1.2;
    ctx.strokeStyle = PALETTE.packetOutline;
    ctx.stroke();
  }
}
