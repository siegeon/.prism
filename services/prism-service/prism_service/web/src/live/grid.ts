/** The canvas ground: flat fill plus the dot grid every PRISM canvas
 * surface sits on (VISUAL_GRAMMAR.md §1). Extracted out of draw.ts so a
 * second canvas page can share the exact same ground without importing
 * draw.ts's whole graph (graphState/cards/hud/gatepanel) — one law, two
 * surfaces, instead of two grids that drift apart. */

import { PALETTE } from "./palette";

export function drawGrid(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  pan: { x: number; y: number },
  zoom: number,
): void {
  ctx.fillStyle = PALETTE.ground;
  ctx.fillRect(0, 0, w, h);
  const step = 28 * zoom;
  if (step < 6) return;
  const offX = (-pan.x * zoom) % step;
  const offY = (-pan.y * zoom) % step;
  ctx.fillStyle = PALETTE.grid;
  for (let x = offX; x < w; x += step) {
    for (let y = offY; y < h; y += step) {
      ctx.fillRect(x, y, 1.4, 1.4);
    }
  }
}
