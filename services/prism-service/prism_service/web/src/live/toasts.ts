/** Docked completion/gate toasts — round 2, piece 3/4 build directive
 * item 2 ("completion/unlock motion... rubric #12"). A small stack of
 * toasts slides in bottom-right, independent of pan/zoom (screen space,
 * same fixed-overlay treatment as hud.ts), and self-prunes after its
 * hold window. State (the list itself) is owned by GraphState, mirroring
 * the packets.ts split: this module only spawns/prunes/draws, never
 * decides WHEN a toast fires (that's a state-transition call made in
 * graphState.ts's applyEvent/reconcile, per the module's own ownership:
 * "graphState.ts (state transitions)").
 *
 * Two kinds only, per the locked palette: "done" (green accent) for a
 * task settling to done, "gate" (magenta accent) for a task's gate
 * flipping to pending — never a third color, so a toast's accent alone
 * tells you which of the two decision-relevant events just happened. */

import { PALETTE } from "./palette";

export type ToastKind = "done" | "gate";

export type Toast = {
  id: string;
  kind: ToastKind;
  text: string;
  createdAt: number;
};

const HOLD_MS = 4000;
const SLIDE_MS = 250;
const FADE_MS = 400;
const TOTAL_MS = HOLD_MS + FADE_MS;

const TOAST_W = 268;
const TOAST_H = 40;
const GAP = 8;
const MARGIN = 16;

let seq = 0;

export function spawnToast(list: Toast[], kind: ToastKind, text: string, now: number): void {
  seq += 1;
  list.push({ id: `${kind}-${now.toFixed(0)}-${seq}`, kind, text, createdAt: now });
}

/** Called once per frame (mirrors packets.ts's stepPackets) — drops any
 * toast whose hold+fade window has elapsed. */
export function pruneToasts(list: Toast[], now: number): Toast[] {
  if (!list.length) return list;
  return list.filter((t) => now - t.createdAt < TOTAL_MS);
}

function easeOutCubic(t: number): number {
  const c = Math.max(0, Math.min(1, t));
  return 1 - Math.pow(1 - c, 3);
}

export function drawToasts(
  ctx: CanvasRenderingContext2D, list: Toast[], screenW: number, screenH: number, now: number,
): void {
  // Newest toast docks lowest (closest to the corner it slides in from),
  // older ones stack upward — matches a typical notification stack.
  let y = screenH - MARGIN - TOAST_H;
  for (let i = list.length - 1; i >= 0; i--) {
    const t = list[i];
    const age = now - t.createdAt;
    const slideFrac = easeOutCubic(Math.min(1, age / SLIDE_MS));
    const x = screenW - MARGIN - TOAST_W + (1 - slideFrac) * (TOAST_W + MARGIN);
    let alpha = 1;
    if (age > HOLD_MS) alpha = Math.max(0, 1 - (age - HOLD_MS) / FADE_MS);

    const accent = t.kind === "done" ? PALETTE.green : PALETTE.magenta;

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.roundRect(x, y, TOAST_W, TOAST_H, 8);
    ctx.fillStyle = "rgba(18,22,32,0.94)";
    ctx.fill();
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.4;
    ctx.stroke();

    // Left accent chip.
    ctx.beginPath();
    ctx.roundRect(x, y, 4, TOAST_H, [8, 0, 0, 8]);
    ctx.fillStyle = accent;
    ctx.fill();

    ctx.fillStyle = PALETTE.textPrimary;
    ctx.font = "600 11px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const maxChars = 32;
    const text = t.text.length > maxChars ? t.text.slice(0, maxChars - 1) + "…" : t.text;
    ctx.fillText(text, x + 14, y + TOAST_H / 2);
    ctx.restore();

    y -= TOAST_H + GAP;
  }
}
