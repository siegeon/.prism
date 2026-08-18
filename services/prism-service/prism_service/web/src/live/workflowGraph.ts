/** The /workflows canvas: the conductor FSM drawn as a wired circuit, with
 * the bots that drive it.
 *
 * Deliberately NOT built on graphState.ts/layout.ts/cards.ts — those model a
 * live session/task board whose nodes appear, move and die on SSE events.
 * This surface has FOURTEEN fixed nodes (10 FSM steps + 4 bots) known up
 * front, so it needs a small state object, not a graph engine. What it DOES
 * share is the visual grammar: routing, wire drawing, in-transit packets,
 * the ground grid and the locked palette all come from the same modules
 * /live draws with, so the two boards can never drift into two dialects.
 */

import { drawGrid } from "./grid";
import { PALETTE, glyphFor } from "./palette";
import { drawPackets, spawnPacket, stepPackets, type Packet } from "./packets";
import { wireKey, type Point } from "./wires";
import {
  WireInteraction, drawEditableWire,
  type PortHit, type SegmentGrab, type WaypointHit, type WireEnd, type WireSurface,
} from "./wireEditing";
import type { Slot } from "./layout";
import type { WorkflowDef } from "@/lib/useWorkflowDef";

const STEP_W = 152, STEP_H = 66, STEP_GAP = 58;
const BOT_W = 148, BOT_H = 58;
const STEP_Y = 300, BOT_Y = 40;

export type WfNode = {
  id: string;
  kind: "step" | "bot";
  /** Human title — the step id / role label, already de-underscored. */
  label: string;
  /** Second line: who owns a step, or the role id for a bot. */
  sub: string;
  glyph: string;
  gate: boolean;
  /** Steps only: how many non-done tasks are standing here right now. */
  count: number;
  slot: Slot;
};

type Wire = {
  key: string;
  from: string;
  to: string;
  /** A bot's ownership of a step is STRUCTURE; the FSM's own progression is
   * the token path work actually travels. */
  kind: "token" | "structure";
  live: boolean;
};

function title(id: string): string {
  return id.replace(/_/g, " ");
}

export class WorkflowGraph {
  nodes: WfNode[] = [];
  wires: Wire[] = [];
  packets: Packet[] = [];
  pan = { x: 0, y: 0 };
  zoom = 1;
  /** How the shared interaction layer reads THIS board. The only wire
   * behaviour that is ours to answer: the FSM chain keeps its plain
   * edge-midpoint elbow while untouched, and this canvas simplifies every
   * route (the /live board opts out — see WireSurface). */
  private readonly wireSurface: WireSurface = {
    keys: () => this.wires.map((w) => w.key),
    ends: (key) => {
      const w = this.wire(key);
      const from = w && this.node(w.from);
      const to = w && this.node(w.to);
      if (!w || !from || !to) return null;
      return { from: from.slot, to: to.slot, fromId: w.from, toId: w.to };
    },
    obstacles: (key) => {
      const w = this.wire(key);
      if (!w) return [];
      return this.nodes.filter((n) => n.id !== w.from && n.id !== w.to).map((n) => n.slot);
    },
    prefersPlainElbow: (key) => this.wire(key)?.kind === "token",
    simplifyUnbentRoutes: true,
  };

  /** Manual wire state — selection, re-docked ends, waypoints — and every
   * hit-test over it. Lives in the shared module so this board and /live
   * cannot drift into two grammars again (task be7a5d2d). */
  readonly wireEdits = new WireInteraction(this.wireSurface);
  /** Manual positions, keyed by node id — persisted per project by the page. */
  private overrides = new Map<string, Point>();
  /** wireKey -> performance.now() the last packet on it arrived, so ambient
   * motion is cycle-spawned (one marker per wire at a time) exactly like the
   * live board, never a stream that reads as spectacle. */
  private lastArrival = new Map<string, number>();
  private fitted = false;

  /** Rebuilds nodes/wires from a fresh /api/workflows payload. Called on
   * every poll — geometry is derived, so re-applying a payload only moves
   * occupancy counts and which wires read live. */
  setDef(def: WorkflowDef): void {
    const steps = def.steps;
    this.nodes = [];

    steps.forEach((s, i) => {
      const gate = s.type === "gate";
      this.nodes.push({
        id: s.id,
        kind: "step",
        label: title(s.id),
        sub: gate ? `${s.persona_label} decides` : s.persona_label,
        glyph: gate ? "◆" : glyphFor("session", s.persona),
        gate,
        count: def.occupancy[s.id] ?? 0,
        slot: this.place(s.id, i * (STEP_W + STEP_GAP), STEP_Y, STEP_W, STEP_H),
      });
    });

    // Bots ride above the chain, spread across its full width so each one's
    // wires drop into roughly the region of the FSM it owns.
    const chainW = Math.max(1, steps.length * (STEP_W + STEP_GAP) - STEP_GAP);
    def.bots.forEach((b, i) => {
      const x = (chainW / def.bots.length) * (i + 0.5) - BOT_W / 2;
      this.nodes.push({
        id: `bot:${b.id}`,
        kind: "bot",
        label: b.persona_label,
        sub: b.id,
        glyph: glyphFor("session", b.id),
        gate: false,
        count: 0,
        slot: this.place(`bot:${b.id}`, x, BOT_Y, BOT_W, BOT_H),
      });
    });

    this.wires = [];
    // The FSM chain, left to right. A segment reads live while work stands
    // at either end of it — that is the stretch of pipeline in use.
    for (let i = 1; i < steps.length; i++) {
      const a = steps[i - 1].id, b = steps[i].id;
      this.wires.push({
        key: wireKey(a, b), from: a, to: b, kind: "token",
        live: (def.occupancy[a] ?? 0) > 0 || (def.occupancy[b] ?? 0) > 0,
      });
    }
    // Each bot to the steps it owns. `persona` (not `agent`) is what carries
    // gate ownership, so the Steward's wires reach its gates too.
    for (const b of def.bots) {
      for (const s of steps) {
        if (s.persona !== b.id) continue;
        const from = `bot:${b.id}`;
        this.wires.push({
          key: wireKey(from, s.id), from, to: s.id, kind: "structure",
          live: (def.occupancy[s.id] ?? 0) > 0,
        });
      }
    }
  }

  private place(id: string, x: number, y: number, w: number, h: number): Slot {
    const o = this.overrides.get(id);
    return { x: o ? o.x : x, y: o ? o.y : y, w, h };
  }

  node(id: string): WfNode | undefined {
    return this.nodes.find((n) => n.id === id);
  }

  /** The wire editor state, under the name the page has always used. */
  get editor() {
    return this.wireEdits.editor;
  }

  /** Current polyline for a wire, re-derived every frame so a dragged node
   * drags its wires (and any packet riding them) along for free. */
  route(w: Wire): Point[] {
    return this.wireEdits.route(w.key);
  }

  /** Settles a wire's stored bends onto clean rails. Called when a drag
   * ends and before persisting, so a saved path never reloads as the
   * staircase the renderer had already smoothed over. */
  settleWire(key: string): void {
    this.wireEdits.settle(key);
  }

  /** The same geometry, one polyline per anchor-to-anchor hop. Drawing
   * joins them; hit-testing needs them apart, because WHICH hop a click
   * landed on is where a new waypoint gets inserted. */
  legs(w: Wire): Point[][] {
    return this.wireEdits.legs(w.key);
  }

  wire(key: string): Wire | undefined {
    return this.wires.find((w) => w.key === key);
  }

  /** The wire whose stroke passes nearest a world point — a real
   * distance-to-segment test, so anywhere along a long run is grabbable,
   * not just near an endpoint. */
  wireAtWorld(wx: number, wy: number): Wire | undefined {
    const key = this.wireEdits.wireAtWorld(wx, wy);
    return key ? this.wire(key) : undefined;
  }

  /** Which hop of `w` a point lands on, and exactly where — the insertion
   * index and position for a new waypoint. */
  legAtWorld(w: Wire, wx: number, wy: number): { leg: number; point: Point } | null {
    return this.wireEdits.legAtWorld(w.key, wx, wy);
  }

  /** Endpoint dots, hit-tested against the polyline the renderer strokes
   * (mx-0a0bf4). Every drawn dot is grabbable — this canvas paints one on
   * every wire, and a handle that is drawn but not grabbable is the same
   * mirror-the-draw violation pointed the other way (task be7a5d2d). */
  portAtWorld(wx: number, wy: number): PortHit | null {
    return this.wireEdits.portAtWorld(wx, wy);
  }

  /** Placed waypoints of the selected wire — handles are drawn only for
   * the selection, so those are the only ones to hit-test. */
  waypointAtWorld(wx: number, wy: number): WaypointHit | null {
    return this.wireEdits.waypointAtWorld(wx, wy);
  }

  /** Index of the polyline run under a world point — which segment a body
   * drag has grabbed. */
  segmentAtWorld(w: Wire, wx: number, wy: number): number | null {
    return this.wireEdits.segmentAtWorld(w.key, wx, wy);
  }

  /** Starts a body drag: resolves the run under the cursor and materializes
   * the anchors it needs. Null when the point isn't on the wire. */
  beginSegmentDrag(key: string, wx: number, wy: number): SegmentGrab | null {
    return this.wireEdits.beginSegmentDrag(key, wx, wy);
  }

  /** World-space slot a wire end is docked to — what a port drag re-docks
   * against. */
  slotForEnd(w: Wire, end: WireEnd): Slot | undefined {
    return this.wireEdits.slotForEnd(w.key, end);
  }

  /** Ambient motion, driven by real occupancy only: a bot->step wire whose
   * step has someone standing on it carries exactly one marker at a time,
   * the next spawning a beat after the previous arrives. A step with no
   * work is silent, so a still canvas honestly means an idle board. */
  step(dtMs: number, now: number): void {
    for (const p of this.packets) {
      const wire = this.wires.find((w) => w.from === p.source && w.to === p.target);
      if (wire) p.pts = this.route(wire);
    }
    const { packets, arrived } = stepPackets(this.packets, dtMs, now);
    this.packets = packets;
    for (const a of arrived) this.lastArrival.set(wireKey(a.source, a.target), now);

    for (const w of this.wires) {
      if (w.kind !== "structure" || !w.live) continue;
      if (this.packets.some((p) => p.source === w.from && p.target === w.to)) continue;
      const last = this.lastArrival.get(w.key) ?? 0;
      if (last && now - last < SPAWN_GAP_MS) continue;
      const pts = this.route(w);
      if (pts.length >= 2) this.packets.push(spawnPacket(w.from, w.to, false, pts));
    }
  }

  // ---- camera + input -----------------------------------------------------

  toWorld(sx: number, sy: number): Point {
    return { x: sx / this.zoom + this.pan.x, y: sy / this.zoom + this.pan.y };
  }

  nodeAtWorld(wx: number, wy: number): WfNode | undefined {
    // Reverse order so the visually topmost node wins a hit.
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const s = this.nodes[i].slot;
      if (wx >= s.x && wx <= s.x + s.w && wy >= s.y && wy <= s.y + s.h) return this.nodes[i];
    }
    return undefined;
  }

  setOverride(id: string, x: number, y: number): void {
    this.overrides.set(id, { x, y });
    const n = this.node(id);
    if (n) { n.slot.x = x; n.slot.y = y; }
  }

  serializeOverrides(): Record<string, Point> {
    return Object.fromEntries(this.overrides);
  }

  hydrateOverrides(raw: Record<string, Point>): void {
    for (const [id, p] of Object.entries(raw ?? {})) {
      if (p && typeof p.x === "number" && typeof p.y === "number") this.setOverride(id, p.x, p.y);
    }
  }

  /** "reset layout": positions AND every manual wire edit. Leaving bent
   * wires behind would make the escape hatch only half-work. */
  clearOverrides(): void {
    this.overrides.clear();
    this.editor.clear();
    this.fitted = false;
  }

  /** Wire keys this board currently has — the drop-unknown-keys filter
   * for rehydrating saved port/waypoint state. */
  wireKeys(): Set<string> {
    return new Set(this.wires.map((w) => w.key));
  }

  /** Frames the whole board once, then leaves the camera to the owner —
   * a view that re-fits under you while you are reading it is unusable. */
  fit(w: number, h: number, force = false): void {
    if (!this.nodes.length || (this.fitted && !force)) return;
    const xs = this.nodes.map((n) => n.slot);
    const minX = Math.min(...xs.map((s) => s.x)) - 40;
    const maxX = Math.max(...xs.map((s) => s.x + s.w)) + 40;
    const minY = Math.min(...xs.map((s) => s.y)) - 40;
    const maxY = Math.max(...xs.map((s) => s.y + s.h)) + 40;
    this.zoom = Math.max(0.35, Math.min(1.4, Math.min(w / (maxX - minX), h / (maxY - minY))));
    this.pan.x = minX - (w / this.zoom - (maxX - minX)) / 2;
    this.pan.y = minY - (h / this.zoom - (maxY - minY)) / 2;
    this.fitted = true;
  }
}

/** Beat between a marker arriving on a wire and the next one departing. */
const SPAWN_GAP_MS = 900;

// ---------------------------------------------------------------------------
// Rendering. Ground -> wires -> packets -> nodes, the same stacking /live
// uses so the two canvases read as one surface.
// ---------------------------------------------------------------------------

export function drawWorkflows(
  ctx: CanvasRenderingContext2D,
  g: WorkflowGraph,
  w: number,
  h: number,
  now: number,
): void {
  drawGrid(ctx, w, h, g.pan, g.zoom);
  ctx.save();
  ctx.scale(g.zoom, g.zoom);
  ctx.translate(-g.pan.x, -g.pan.y);

  for (const wire of g.wires) {
    const selected = g.editor.selected === wire.key;
    // The selected-wire paint (orange body, orange endpoint dots, hollow
    // bend handles) lives in the shared renderer, so this canvas and
    // /live's draw.ts cannot drift into two oranges (task be7a5d2d).
    drawEditableWire(ctx, {
      pts: g.route(wire),
      kind: wire.kind,
      live: wire.live,
      selected,
      waypoints: selected ? g.editor.waypointsFor(wire.key) : undefined,
    });
  }
  drawPackets(ctx, g.packets, now);
  for (const n of g.nodes) drawNode(ctx, n);

  ctx.restore();
}

function drawNode(ctx: CanvasRenderingContext2D, n: WfNode): void {
  const { x, y, w, h } = n.slot;

  ctx.fillStyle = PALETTE.card;
  ctx.fillRect(x, y, w, h);

  // A gate is a DECISION, not a unit of work — magenta is the locked hue for
  // "needs a distinct actor" (palette.ts), so gates never read as just
  // another agent step.
  ctx.fillStyle = n.gate ? "#3a2f45" : n.kind === "bot" ? "#2c3550" : PALETTE.cardTitle;
  ctx.fillRect(x, y, w, 20);

  ctx.strokeStyle = n.gate ? PALETTE.magenta : PALETTE.border;
  ctx.lineWidth = n.gate ? 1.5 : 1;
  ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);

  ctx.font = "12px ui-monospace, SFMono-Regular, monospace";
  ctx.textBaseline = "middle";
  ctx.fillStyle = PALETTE.textLabel;
  ctx.fillText(n.glyph, x + 8, y + 10);

  ctx.font = "600 12px ui-sans-serif, system-ui, sans-serif";
  ctx.fillStyle = PALETTE.textPrimary;
  ctx.fillText(clip(ctx, n.label, w - 34), x + 24, y + 10);

  ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
  ctx.fillStyle = PALETTE.textDim;
  ctx.fillText(clip(ctx, n.sub, w - 20), x + 10, y + h / 2 + 12);

  if (n.count > 0) drawOccupancy(ctx, n);
}

/** The count badge: how many tasks are standing on this step right now.
 * Orange is the locked hue for step progress / compute in flight. */
function drawOccupancy(ctx: CanvasRenderingContext2D, n: WfNode): void {
  const cx = n.slot.x + n.slot.w - 16, cy = n.slot.y + n.slot.h - 16;
  ctx.beginPath();
  ctx.arc(cx, cy, 11, 0, Math.PI * 2);
  ctx.fillStyle = PALETTE.orange;
  ctx.fill();
  ctx.font = "700 12px ui-sans-serif, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.fillStyle = PALETTE.packetOutline;
  ctx.fillText(String(n.count), cx, cy + 1);
  ctx.textAlign = "left";
}

function clip(ctx: CanvasRenderingContext2D, text: string, maxW: number): string {
  if (ctx.measureText(text).width <= maxW) return text;
  let out = text;
  while (out.length > 1 && ctx.measureText(`${out}…`).width > maxW) out = out.slice(0, -1);
  return `${out}…`;
}
