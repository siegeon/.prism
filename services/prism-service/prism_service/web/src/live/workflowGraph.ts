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

const STEP_W = 184, STEP_H = 88, STEP_GAP = 58;
const BOT_W = 148, BOT_H = 58;
const STEP_Y = 300, BOT_Y = 40;

export type WfNode = {
  id: string;
  kind: "step" | "bot";
  /** Human title — the step id / role label, already de-underscored. */
  label: string;
  /** Second line: who owns a step, or the role id for a bot. */
  sub: string;
  /** Progressive-disclosure preview: the child/transition below this state. */
  summary: string;
  /** Linked workflows are behavior handoffs, not ordinary detail drill-ins. */
  actionLabel?: string;
  childCount?: number;
  glyph: string;
  gate: boolean;
  /** Steps only: how many non-done tasks are standing here right now. */
  count: number;
  slot: Slot;
};

export type ActiveNodeProgress = {
  nodeId: string;
  /** 0..0.98 while running; completion is represented by leaving the node. */
  progress: number;
  indeterminate: boolean;
  elapsedSeconds: number;
  averageSeconds: number | null;
  /** Historical playback names itself instead of pretending to execute. */
  label?: string;
  /** Recorded outcome color for historical playback. */
  tone?: "active" | "success" | "failure" | "warning";
};

/** The REAL per-node answer of a drilled-in behaviour layer, for one task,
 * as GET /api/workflows/{id}/node-status reports it.
 *
 * A behaviour layer has no WorkflowCore run behind it, so `ActiveNodeProgress`
 * is always null there and the canvas drew a dead diagram (owner 2026-08-29:
 * "there is no indication anywhere what the hell is going on"). Each node of
 * such a layer IS a check with an answer, and this is that answer. It is a
 * VERDICT, never a progress reading: nothing here animates and nothing here
 * is measured against a clock. */
export type NodeVerdict = {
  state: "passed" | "refused" | "not_reached" | "unknown";
  reason?: string;
};

type VerdictPaint = {
  /** Border and label colour. */
  stroke: string;
  /** The word drawn where a running node draws its elapsed clock. */
  label: string;
  /** A check that RAN gets a full-width, STATIC completion rail. A check
   * that never ran gets NONE -- a partial or moving rail is a claim about
   * how far along something is, and there is no measurement behind it. The
   * 18-second wall-clock sawtooth removed in 7.13.174/175 is exactly the
   * mistake this rule exists to prevent. */
  rail: boolean;
  /** Never-ran states are drawn back, so a node inference never reached can
   * never be mistaken for one that passed. */
  dim: boolean;
};

function verdictPaint(verdict: NodeVerdict): VerdictPaint {
  switch (verdict.state) {
    case "passed":
      return { stroke: "#34d399", label: "PASSED", rail: true, dim: false };
    case "refused":
      return { stroke: "#f87171", label: "REFUSED", rail: true, dim: false };
    case "not_reached":
      return { stroke: PALETTE.border, label: "NOT REACHED", rail: false, dim: true };
    default:
      return { stroke: PALETTE.border, label: "UNKNOWN", rail: false, dim: true };
  }
}

function shortDuration(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(whole / 60);
  const remainder = whole % 60;
  return minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

type Wire = {
  key: string;
  from: string;
  to: string;
  /** A bot's ownership of a step is STRUCTURE; the FSM's own progression is
   * the token path work actually travels. */
  kind: "token" | "structure";
  live: boolean;
  /** A step's exit condition belongs to its outgoing transition. */
  label?: string | null;
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
  /** prefers-reduced-motion: OS-level, set once by the page from
   * matchMedia and kept live on change. While true, no packet ever rides
   * a wire -- a transition still lands (the occupied node's own state
   * changes instantly, same frame), it just never travels there. Never a
   * broken layout, never a frozen-mid-flight marker. */
  private reducedMotion = false;
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

    this.nodes.push({
      id: "__start__", kind: "step", label: "Start", sub: "Entry state",
      summary: steps[0] ? `Next · ${title(steps[0].id)}` : "No child state",
      glyph: "▶", gate: false, count: def.occupancy.__start__ ?? 0,
      slot: this.place("__start__", 0, STEP_Y, STEP_W, STEP_H),
    });

    steps.forEach((s, i) => {
      const gate = s.type === "gate";
      const linkedWorkflowLabel = s.linked_workflow_id === "validation"
        ? "Build and test"
        : s.linked_workflow_id ? title(s.linked_workflow_id) : null;
      this.nodes.push({
        id: s.id,
        kind: "step",
        label: linkedWorkflowLabel ?? title(s.id),
        sub: gate ? `${s.persona_label} decides` : s.action || s.persona_label,
        summary: linkedWorkflowLabel
          ? `${linkedWorkflowLabel} workflow`
          : i + 1 < steps.length
            ? `Next · ${title(steps[i + 1].id)}`
            : "Next · Complete",
        actionLabel: linkedWorkflowLabel ? "⌄" : "↗",
        childCount: s.linked_workflow_step_count,
        glyph: gate ? "◆" : glyphFor("session", s.persona),
        gate,
        count: def.occupancy[s.id] ?? 0,
        slot: this.place(s.id, (i + 1) * (STEP_W + STEP_GAP), STEP_Y, STEP_W, STEP_H),
      });
    });

    this.nodes.push({
      id: "__complete__", kind: "step", label: "Complete", sub: "Terminal state",
      summary: "Workflow finished", glyph: "■", gate: false,
      count: def.occupancy.__complete__ ?? 0,
      slot: this.place("__complete__", (steps.length + 1) * (STEP_W + STEP_GAP), STEP_Y, STEP_W, STEP_H),
    });

    // Each bot's label left-aligns to the leftmost step it actually owns --
    // the SAME x a step node gets from `this.place` below, not a guess at
    // one. An even count-based spread (the previous approach) put a bot
    // wherever its INDEX among bots happened to land, with no relationship
    // to which columns its wires actually drop into -- a bot owning only
    // late steps floated over empty canvas with no visible column beneath
    // it. A bot that owns zero steps (real today: "architect" is a defined
    // bot with no conductor step carrying persona="architect") gets no
    // node at all -- the wire loop below already draws it zero wires, so a
    // label with nothing under it would just be a second unanchored float,
    // the exact defect this fix removes for the other bots.
    def.bots.forEach((b) => {
      const ownedIndices = steps
        .map((s, si) => (s.persona === b.id ? si : -1))
        .filter((si) => si >= 0);
      if (!ownedIndices.length) return;
      const x = (Math.min(...ownedIndices) + 1) * (STEP_W + STEP_GAP);
      this.nodes.push({
        id: `bot:${b.id}`,
        kind: "bot",
        label: b.persona_label,
        sub: b.id,
        summary: "Owns workflow states",
        glyph: glyphFor("session", b.id),
        gate: false,
        count: 0,
        slot: this.place(`bot:${b.id}`, x, BOT_Y, BOT_W, BOT_H),
      });
    });

    this.wires = [];
    const stateIds = ["__start__", ...steps.map((s) => s.id), "__complete__"];
    // The FSM chain, left to right. A segment reads live while work stands
    // at either end of it — that is the stretch of pipeline in use.
    for (let i = 1; i < stateIds.length; i++) {
      const a = stateIds[i - 1], b = stateIds[i];
      this.wires.push({
        key: wireKey(a, b), from: a, to: b, kind: "token",
        live: (def.occupancy[a] ?? 0) > 0 || (def.occupancy[b] ?? 0) > 0,
        label: i > 1 ? steps[i - 2]?.validation : null,
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

  /** Read by the page from matchMedia("(prefers-reduced-motion: reduce)")
   * and kept in sync on change. Clears any packets already in flight so
   * the switch takes effect immediately, not just for the next spawn. */
  setReducedMotion(reduced: boolean): void {
    this.reducedMotion = reduced;
    if (reduced) this.packets = [];
  }

  /** Ambient motion, driven by real occupancy only: a bot->step wire whose
   * step has someone standing on it carries exactly one marker at a time,
   * the next spawning a beat after the previous arrives. A step with no
   * work is silent, so a still canvas honestly means an idle board. */
  step(dtMs: number, now: number): void {
    if (this.reducedMotion) return;
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

  /** Inject one visible work item onto an FSM transition. Unlike ambient
   * occupancy motion, this is called deliberately by the workflow simulator
   * and therefore applies only to the left-to-right token chain. */
  sendTransition(source: string, target: string): boolean {
    const wire = this.wires.find((candidate) => (
      candidate.kind === "token"
      && candidate.from === source
      && candidate.to === target
    ));
    if (!wire) return false;
    const pts = this.route(wire);
    if (pts.length < 2) return false;
    this.packets = this.packets.filter((packet) => !(
      packet.source === source && packet.target === target
    ));
    // Reduced motion: the transition is real (the caller's own node
    // state flips this same frame) -- it just never rides a marker to
    // get there, per the craft brief's "instant state changes, never a
    // broken layout."
    if (!this.reducedMotion) this.packets.push(spawnPacket(source, target, false, pts));
    return true;
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
  selectedNodeId: string | null = null,
  activeProgress: ActiveNodeProgress | null = null,
  nodeVerdicts: Record<string, NodeVerdict> | null = null,
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
    if (wire.kind === "token" && wire.label) {
      const route = g.route(wire);
      const from = route[0], to = route[route.length - 1];
      if (from && to) drawTransitionLabel(ctx, wire.label, {
        x: (from.x + to.x) / 2,
        y: (from.y + to.y) / 2,
      });
    }
  }
  drawPackets(ctx, g.packets, now);
  for (const n of g.nodes) drawNode(
    ctx, n, n.id === selectedNodeId,
    activeProgress?.nodeId === n.id ? activeProgress : null,
    nodeVerdicts?.[n.id] ?? null,
  );

  ctx.restore();
}

function drawTransitionLabel(ctx: CanvasRenderingContext2D, label: string, at: Point): void {
  ctx.font = "9px ui-monospace, SFMono-Regular, monospace";
  const text = clip(ctx, label, 116);
  const width = ctx.measureText(text).width + 10;
  ctx.fillStyle = PALETTE.ground;
  ctx.fillRect(at.x - width / 2, at.y - 8, width, 16);
  ctx.fillStyle = PALETTE.textLabel;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, at.x, at.y);
  ctx.textAlign = "left";
}

function drawNode(ctx: CanvasRenderingContext2D, n: WfNode, selected = false, active: ActiveNodeProgress | null = null, verdict: NodeVerdict | null = null): void {
  const { x, y, w, h } = n.slot;

  // A live run wins: `active` is what is happening RIGHT NOW, a verdict is
  // what a check already answered. They never coincide on a behaviour layer
  // (no WorkflowCore run backs one), so this is an ordering rule, not a
  // conflict.
  const verdictLook = !active && verdict ? verdictPaint(verdict) : null;
  if (verdictLook?.dim) {
    ctx.save();
    ctx.globalAlpha = 0.45;
  }

  if (n.childCount) {
    ctx.fillStyle = PALETTE.cardTitle;
    ctx.fillRect(x + 4, y + 4, w, h);
    ctx.strokeStyle = PALETTE.border;
    ctx.strokeRect(x + 4.5, y + 4.5, w - 1, h - 1);
  }
  ctx.fillStyle = PALETTE.card;
  ctx.fillRect(x, y, w, h);

  // Establish stable card chrome.
  ctx.fillStyle = n.gate ? "#3a2f45" : n.kind === "bot" ? "#2c3550" : PALETTE.cardTitle;
  ctx.fillRect(x, y, w, 20);

  if (verdictLook?.rail) {
    // FULL WIDTH and STILL. A check that ran is finished, not in progress:
    // there is no width here to interpolate and no frame in which this
    // moves.
    ctx.fillStyle = verdictLook.stroke;
    ctx.fillRect(x + 1, y + 1, w - 2, 3);
  }

  if (active) {
    const fillWidth = Math.max(3, (w - 2) * active.progress);
    const rgb = active.tone === "failure" ? "248, 113, 113"
      : active.tone === "success" ? "52, 211, 153"
        : active.tone === "warning" ? "252, 211, 77" : "39, 201, 180";
    ctx.fillStyle = "rgba(255,255,255,0.10)";
    ctx.fillRect(x + 1, y + 1, w - 2, 3);
    ctx.fillStyle = `rgba(${rgb}, 0.95)`;
    ctx.fillRect(x + 1, y + 1, fillWidth, 3);
    // The card's own body fills left-to-right over the step's
    // elapsed/average duration -- the actual playback a person watches,
    // not just the thin header rail above (owner, live, pointing at a
    // card's body: "a bar in the body of the panel filling over time").
    // Painted before the glyph/label/sub/summary text below so the fill
    // always sits under, never over, the card's own content.
    // An INDETERMINATE step paints no body fill at all. A width here is a
    // claim about how far along the step is, and with no duration history
    // there is nothing to base that claim on -- the old caller fed this a
    // wall-clock sawtooth and the card visibly filled and reset every 18s
    // while no work happened. The elapsed clock on the card still says it
    // is running.
    const bodyY = y + 20, bodyH = h - 20;
    if (!active.indeterminate) {
      ctx.fillStyle = `rgba(${rgb}, 0.16)`;
      ctx.fillRect(x + 1, bodyY, fillWidth, bodyH - 1);
      // A brighter leading edge marks exactly how far the fill has reached,
      // the same "where is it right now" cue the header rail's hard right
      // edge already gives. It is a POSITION claim, so it is guarded by the
      // same condition as the fill it belongs to.
      ctx.fillStyle = `rgba(${rgb}, 0.55)`;
      ctx.fillRect(x + 1 + Math.max(0, fillWidth - 2), bodyY, 2, bodyH - 1);
    }
  }

  // A gate is a DECISION, not a unit of work — magenta is the locked hue for
  // "needs a distinct actor" (palette.ts), so gates never read as just
  // another agent step.
  const activeStroke = active?.tone === "failure" ? "#f87171"
    : active?.tone === "success" ? "#34d399"
      : active?.tone === "warning" ? "#fcd34d" : PALETTE.teal;
  ctx.strokeStyle = active ? activeStroke
    : verdictLook ? verdictLook.stroke
      : n.gate ? PALETTE.magenta : PALETTE.border;
  ctx.lineWidth = active || n.gate || verdictLook ? 1.5 : 1;
  ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
  if (selected) {
    ctx.strokeStyle = PALETTE.teal;
    ctx.lineWidth = 2;
    ctx.strokeRect(x - 2, y - 2, w + 4, h + 4);
  }

  ctx.font = "12px ui-monospace, SFMono-Regular, monospace";
  ctx.textBaseline = "middle";
  ctx.fillStyle = PALETTE.textLabel;
  ctx.fillText(n.glyph, x + 8, y + 10);

  ctx.font = "600 12px ui-sans-serif, system-ui, sans-serif";
  ctx.fillStyle = PALETTE.textPrimary;
  ctx.fillText(clip(ctx, n.label, w - 104), x + 24, y + 10);
  ctx.textAlign = "right";
  ctx.font = "10px ui-monospace, SFMono-Regular, monospace";
  ctx.fillStyle = active ? activeStroke
    : verdictLook ? verdictLook.stroke
      : selected ? PALETTE.teal : PALETTE.textLabel;
  const runLabel = active
    ? active.label ?? (active.averageSeconds
      ? active.elapsedSeconds <= active.averageSeconds
        ? `RUN ${shortDuration(active.elapsedSeconds)} / ~${shortDuration(active.averageSeconds)}`
        : `RUN ${shortDuration(active.elapsedSeconds)}`
      : `RUN ${shortDuration(active.elapsedSeconds)}`)
    : verdictLook ? verdictLook.label
      : n.childCount ? `${n.childCount} ${n.actionLabel}` : n.actionLabel ?? "↗";
  ctx.fillText(runLabel, x + w - 8, y + 10);
  ctx.textAlign = "left";

  ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
  ctx.fillStyle = PALETTE.textDim;
  ctx.fillText(clip(ctx, n.sub, w - 20), x + 10, y + h / 2 + 12);

  ctx.font = "10px ui-monospace, SFMono-Regular, monospace";
  ctx.fillStyle = PALETTE.textLabel;
  ctx.fillText(clip(ctx, n.summary, w - 20), x + 10, y + h - 12);

  // Active progress already visualizes the one token occupying this state.
  // Showing the numeric badge at the same time duplicates that signal and,
  // during replay, looks like an error count rather than occupancy.
  if (n.count > 0 && !active) drawOccupancy(ctx, n);

  if (verdictLook?.dim) ctx.restore();
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
