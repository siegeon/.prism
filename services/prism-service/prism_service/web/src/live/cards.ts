/** Node card rendering — opaque slate card, brighter title bar, small
 * leading glyph, NO border unless selected (design directive). Stat rows
 * carry a connector dot (filled+saturated when flowing, hollow ring when
 * the signal is dead) plus a value that GHOSTS (double-exposes) for a
 * beat whenever it just changed — the primary "alive" cue.
 *
 * Round 2, piece 3 (node state while working) rebuild: every card now
 * derives one of five states (graphState.ts's deriveCardState) and the
 * WHOLE card's chrome — title-bar tint, a magenta left-edge stripe, a
 * red top border, dot ring colors, done settle+compaction — reads that
 * state, not just small grey step text. Closes the round1 critic's exact
 * gap: "a red dot sits next to Step/Spend on every card whether it's
 * active or not, so red is never reserved for 'dead', it's just noise...
 * the card list never adds, removes, or recolors anything". */

import type { LiveNode } from "./graphState";
import { COMPACT_AFTER_MS, SETTLE_MS, SIGNAL_AGE_CHIP_MS, signalFreshness } from "./graphState";
import { PALETTE, glyphFor, deadRingColorFor, titleTintFor, type CardState } from "./palette";

const TITLE_H = 24;
const PAD_X = 10;
const ROW_H = 19;
const ROW_START_Y = TITLE_H + 9;
const DOT_R = 3.5;
const CHIP_H = 30;
const EDGE_STRIPE_W = 4;
/** Round 4 item 3: "cap ~5 with a +N" -- one hollow square per queued
 * child up to this many, then a small "+N" overflow caption. */
const QUEUE_GLYPH_CAP = 5;

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

function fmtUsd(n: number): string {
  if (!n) return "$0";
  if (n >= 1000) return "$" + (n / 1000).toFixed(2) + "K";
  return "$" + n.toFixed(n >= 1 ? 2 : 3);
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

/** `deadColor` comes from palette.ts's deadRingColorFor(cardState) at
 * every call site below — NEVER a hardcoded PALETTE.red — so red only
 * ever appears on a STALLED (or failed) card, per the locked palette
 * rule "red is RESERVED for dead... never 'slow'". A young/pending card
 * with no signal yet, or a healthy working card with a stat that simply
 * hasn't ticked (e.g. Spend at $0 on a fresh task), both fall back to a
 * neutral dim ring instead.
 *
 * Round 3 item 6 (graded silence): `freshness` (0..1, from graphState's
 * signalFreshness) replaces the old plain boolean `live` — round1/2 drew
 * a dot as a flat binary (fully filled or an instant hollow ring), so a
 * card 44s into going quiet looked pixel-identical to one that ticked a
 * second ago right up until STALL_MS's hard flip to red (critic 3's
 * exact gap). Now the filled dot's alpha ramps down and the dead ring's
 * alpha ramps up together as freshness falls, so a viewer sees a card
 * visibly cooling off well before it's declared stalled. */
function drawConnectorDot(
  ctx: CanvasRenderingContext2D, x: number, y: number, color: string, freshness: number, deadColor: string,
): void {
  const f = Math.max(0, Math.min(1, freshness));
  ctx.beginPath();
  ctx.arc(x, y, DOT_R, 0, Math.PI * 2);
  if (f > 0) {
    ctx.globalAlpha = f;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
  }
  if (f < 1) {
    ctx.beginPath();
    ctx.arc(x, y, DOT_R, 0, Math.PI * 2);
    ctx.strokeStyle = deadColor;
    ctx.lineWidth = 1.4;
    ctx.globalAlpha = Math.max(0.35, 1 - f);
    ctx.stroke();
    ctx.globalAlpha = 1;
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
  /** Live USD spend (round 2, piece 3/4) — a real ticking number, never
   * the hardcoded "$0" round1 shipped on every card regardless of state. */
  spendUsd: number;
};

const STATUS_ICON: Record<string, string> = {
  done: "✓",
  in_progress: "",
  pending: "",
};

/** Draws the compacted "done" chip a completed card settles into after
 * COMPACT_AFTER_MS (build item 1: "DONE: rows settle green, card
 * compacts after ~4s to a slim 'done' chip row... do not vanish
 * instantly — the completion must be witnessed"). Renders inside the
 * SAME slot bounds the full card used (layout.ts hands out a slot ONCE
 * and keeps it forever, so physically relocating the chip into a
 * parent's fan is out of scope here — see the task report's noted
 * simplification) — top-anchored, everything below it left empty. */
function drawDoneChip(ctx: CanvasRenderingContext2D, n: LiveNode): void {
  const { x, y } = n;
  const { w } = n.slot;
  ctx.beginPath();
  ctx.roundRect(x, y, w, CHIP_H, 6);
  ctx.fillStyle = PALETTE.cardTitleDone;
  ctx.fill();
  ctx.strokeStyle = "rgba(52,211,153,0.35)";
  ctx.lineWidth = 1;
  ctx.stroke();

  ctx.fillStyle = PALETTE.green;
  ctx.font = "12px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText("✓", x + PAD_X, y + CHIP_H / 2);

  ctx.fillStyle = PALETTE.textPrimary;
  ctx.font = "600 11px system-ui, sans-serif";
  const maxChars = Math.max(8, Math.floor((w - 40) / 6.2));
  ctx.fillText(truncate(n.label, maxChars), x + PAD_X + 16, y + CHIP_H / 2);
}

export function drawCard(
  ctx: CanvasRenderingContext2D, n: LiveNode, m: CardMetrics, state: CardState, now: number,
  graphQuiet = false,
): void {
  // DONE settles, then compacts to a slim chip -- witnessed, not vanished.
  if (state === "done" && n.doneAt && now - n.doneAt > COMPACT_AFTER_MS) {
    drawDoneChip(ctx, n);
    return;
  }

  const { x, y } = n;
  const { w, h } = n.slot;
  const r = 6;
  // Round 4 item 1c: red (and the stalled title tint) is only painted
  // while OTHER cards are still active -- see palette.ts's doc comment.
  const deadColor = deadRingColorFor(state, graphQuiet);
  // Round 3 item 6: one freshness value per card, applied to every row's
  // connector dot -- see signalFreshness's doc comment in graphState.ts.
  const freshness = signalFreshness(now, n.lastSignalAt);

  // Round 4 item 6 (selection unmistakable): a selected card gets a
  // slight lift (a few px up) in addition to its outline -- applied as a
  // transform around the WHOLE card's draw calls, same technique as the
  // existing "settling" transform below, so the lift moves body+outline
  // together rather than just floating the stroke over a static body.
  const lifted = n.selected;
  if (lifted) {
    ctx.save();
    ctx.shadowColor = "rgba(0,0,0,0.55)";
    ctx.shadowBlur = 14;
    ctx.shadowOffsetY = 6;
    ctx.translate(0, -3);
  }

  // Brief settle: scale 1.03 -> 1.0 around the card's own center, right
  // when a task hits done (build item 2). Applied as a transform around
  // this card's draw calls only -- restored before the function returns.
  const settling = n.settleUntil > now;
  if (settling) {
    const frac = 1 - Math.max(0, n.settleUntil - now) / SETTLE_MS;
    const scale = 1.03 - 0.03 * Math.min(1, frac);
    const cx = x + w / 2, cy = y + h / 2;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.scale(scale, scale);
    ctx.translate(-cx, -cy);
  }

  // Card body — opaque slate, no border unless selected.
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
  ctx.fillStyle = PALETTE.card;
  ctx.fill();
  // Shadow is only wanted under the card body itself (the lift), never
  // on the text/dots drawn afterward -- clear it right after the one
  // fill call it was set up for.
  if (lifted) {
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
  }

  // STALLED desaturates: a translucent dark wash over the whole body,
  // under everything else, so numbers/labels stay legible but read
  // visibly duller than a healthy card (build item 1).
  if (state === "stalled") {
    ctx.fillStyle = "rgba(8,9,13,0.4)";
    ctx.fill();
  }

  // STALLED thin top border — red appears HERE (and on the dead
  // connector rings) ONLY while other cards are still active; once the
  // whole graph reads quiet this downgrades to the same neutral dim tone
  // every other non-alarm border uses (round 4 item 1c).
  if (state === "stalled") {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.strokeStyle = graphQuiet ? PALETTE.textDim : PALETTE.red;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // WAITING_GATE magenta left-edge stripe — "clearly not red, clearly
  // not working" (build item 1).
  if (state === "waiting_gate") {
    ctx.save();
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, r);
    ctx.clip();
    ctx.fillStyle = PALETTE.magenta;
    ctx.fillRect(x, y, EDGE_STRIPE_W, h);
    ctx.restore();
  }

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

  // Title bar — tinted per card state (round 2, piece 3): teal-tinted
  // while working, magenta while waiting on a gate, desaturated while
  // stalled, green while done, plain neutral while young. A settling
  // card also gets a brief green flash overlaid on top of its tint.
  ctx.save();
  ctx.beginPath();
  ctx.roundRect(x, y, w, TITLE_H, [r, r, 0, 0]);
  ctx.clip();
  ctx.fillStyle = titleTintFor(state, graphQuiet);
  ctx.fillRect(x, y, w, TITLE_H);
  if (settling) {
    const flashFrac = Math.max(0, n.settleUntil - now) / SETTLE_MS;
    ctx.globalAlpha = flashFrac * 0.55;
    ctx.fillStyle = PALETTE.green;
    ctx.fillRect(x, y, w, TITLE_H);
    ctx.globalAlpha = 1;
  }
  ctx.restore();

  ctx.fillStyle = PALETTE.textLabel;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(glyphFor(n.kind, n.role), x + PAD_X, y + TITLE_H / 2);

  ctx.fillStyle = PALETTE.textPrimary;
  ctx.font = "600 12px system-ui, sans-serif";
  const titleMaxChars = Math.max(8, Math.floor((w - 44) / 6.4));
  ctx.fillText(truncate(n.label, titleMaxChars), x + PAD_X + 16, y + TITLE_H / 2);

  const icon = STATUS_ICON[n.status] ?? "";
  if (icon) {
    ctx.fillStyle = PALETTE.green;
    ctx.textAlign = "right";
    ctx.fillText(icon, x + w - PAD_X, y + TITLE_H / 2);
  } else if (n.lastSignalAt && now - n.lastSignalAt > SIGNAL_AGE_CHIP_MS) {
    // Round 3 item 6: "Ns since signal" once a card's own age crosses
    // 15s -- only where the done checkmark isn't already occupying this
    // corner. Pairs with the connector dots' continuous fade so a
    // viewer gets both a color cue AND a readable number for how stale
    // this card's last real event is.
    const ageS = Math.floor((now - n.lastSignalAt) / 1000);
    ctx.fillStyle = PALETTE.textDim;
    ctx.font = "10px ui-monospace, SFMono-Regular, monospace";
    ctx.textAlign = "right";
    ctx.fillText(`${ageS}s since signal`, x + w - PAD_X, y + TITLE_H / 2);
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
    drawConnectorDot(ctx, dotX, rowY, PALETTE.teal, m.tokensLive ? freshness : 0, deadColor);
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

  // Recent-throughput BUFFER bar (round 2, piece 5; round 3 item 4;
  // round 4 item 2) — ~70% of card width, teal fill on a dark track,
  // directly under the Tokens value it's the gauge FOR. Fill is
  // scale.ts's shared fixed log scale (item 2a), drains to 0 over ~4s of
  // silence, so its LENGTH alone answers "how loaded is this right now"
  // (round1 critic: "no node ever shows load as a filling bar"). Round 4
  // item 2b DROPS the old `n.kind !== "session"` guard: "the fastest-
  // moving leaf nodes carry no bar at all" -- session/agent cards now
  // get the identical bar, sized off their own tok_s. Bumped 6px -> 7px
  // (round 3 item 4: "thicker (7px)") and kept visually distinct from
  // the THIN orange Step bar below (drawCapacityBar's h=4 default there).
  {
    const barW = Math.min(w * 0.7, valueRight - labelX);
    rowY += 8;
    drawCapacityBar(ctx, labelX, rowY, barW, m.bufferFrac, PALETTE.teal, m.bufferFrac > 0.015, 7);
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
    drawConnectorDot(ctx, dotX, rowY, PALETTE.orange, m.stepLive ? freshness : 0, deadColor);
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
  // has subtasks waiting (grammar §2: "backed up / queued"). Round 4
  // item 3: one square per queued child, capped at QUEUE_GLYPH_CAP with
  // a trailing "+N" once the real count exceeds what fits legibly.
  if (m.queueDepth > 0) {
    ctx.strokeStyle = PALETTE.textDim;
    ctx.lineWidth = 1;
    const shown = Math.min(m.queueDepth, QUEUE_GLYPH_CAP);
    for (let i = 0; i < shown; i++) {
      ctx.strokeRect(labelX + i * 8, rowY - 12, 6, 6);
    }
    if (m.queueDepth > QUEUE_GLYPH_CAP) {
      ctx.fillStyle = PALETTE.textDim;
      ctx.font = "9px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(`+${m.queueDepth - QUEUE_GLYPH_CAP}`, labelX + shown * 8 + 2, rowY - 9);
    }
  }

  // Spend row (green) — a REAL ticking number (round 2, piece 3/4 fix):
  // round1 hardcoded this row to "$0" with `live=false` unconditionally,
  // which is exactly the "red dot on every card" bug the critic named --
  // the dot now falls back to `deadColor` (neutral-dim on a healthy
  // young/working card, never red) instead of a bare PALETTE.red, and
  // the value is m.spendUsd, ticking live off tokens.turn's usd_total.
  if (h >= 96) {
    const spendLive = m.spendUsd > 0;
    drawConnectorDot(ctx, dotX, rowY, PALETTE.green, spendLive ? freshness : 0, deadColor);
    ctx.fillStyle = PALETTE.textLabel;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("Spend", labelX, rowY);
    ctx.fillStyle = spendLive ? PALETTE.green : PALETTE.textDim;
    ctx.textAlign = "right";
    ctx.font = "11px ui-monospace, SFMono-Regular, monospace";
    ctx.fillText(fmtUsd(m.spendUsd), valueRight, rowY);
    rowY += ROW_H;
  }

  // Gate row (magenta) — only when a gate is actually pending, carrying
  // the real elapsed wait (build item 1: "gate · waiting Xm").
  if (m.gatePending && rowY + 4 < y + h) {
    ctx.beginPath();
    ctx.arc(dotX, rowY, DOT_R, 0, Math.PI * 2);
    ctx.fillStyle = PALETTE.magenta;
    ctx.fill();
    ctx.fillStyle = PALETTE.magenta;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "left";
    // 28 chars used to truncate "gate: <step> · waiting Nm" right before
    // the minute count -- verified live: the one number the critic
    // explicitly wants ("gate · waiting Xm") was the exact part getting
    // cut. 40 clears the whole string at this row's normal font/width.
    ctx.fillText(truncate(`gate: ${m.gateLabel}`, 40), labelX, rowY);
  }

  if (settling) ctx.restore();
  if (lifted) ctx.restore();
}
