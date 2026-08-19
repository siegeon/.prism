/** Direct manipulation of a wire on EITHER canvas — select it, re-dock
 * either endpoint, bend the middle (owner at green_gate review: "I should
 * be able to click on them like draw.io / mxGraph so that I can move this
 * and help ensure their path is the way I want it to be").
 *
 * This grammar grew on /workflows (task f506ece4) and the /live board had
 * none of it, which the owner read as one component that had drifted:
 * "we modified the wire behavior on the workflows screen, but that is a
 * shared component with the live screen — or should be". So the layer
 * lives HERE, canvas-agnostic, and both boards consume it (task
 * be7a5d2d). It was called workflowWires.ts; a module named for one
 * canvas but imported by both is exactly the name that invites the drift
 * back.
 *
 * A canvas plugs in by implementing WireSurface below — which slots a
 * wire spans, what it should avoid, and two policy answers. Everything
 * else (selection, waypoints, segment grabs, hit-test ordering, the
 * simplify pass, the selected-wire paint) is owned here, once.
 *
 * State + geometry only; each canvas owns its nodes and wires, and its
 * page owns the gestures. Split out so workflowGraph.ts does not grow
 * into the god-module /live's graphState.ts already is.
 *
 * Two things this module deliberately does NOT own:
 *  - the router. Every hop — node->waypoint, waypoint->waypoint,
 *    waypoint->node — goes through live/wires.ts's routeOrthogonal, so a
 *    bent wire keeps the same 90-degree grammar as a straight one. A
 *    second path builder here is exactly how the two canvases would drift
 *    into two dialects.
 *  - the port math. A re-dock resolves through portFromWorld/portPoint,
 *    the same functions the renderer uses (the /live lesson mx-0a0bf4:
 *    the hit-test must MIRROR the draw, never re-derive it).
 *
 * Override keys mirror graphState.ts exactly — a flat map keyed
 * `<wireKey>:from` / `<wireKey>:to` — so the two boards' persisted shapes
 * stay readable as one scheme.
 */

import {
  autoPort, drawPort, drawWire, laneFor, portFromWorld, routeOrthogonal, wireColor,
  type Point, type WireKind, type WirePort,
} from "./wires";
import { PALETTE } from "./palette";
import type { Slot } from "./layout";

export type WireEnd = "from" | "to";

/** A hit on a wire's endpoint dot: which wire, which end, and the node
 * that end is docked to. Mirrors graphState.ts's PortHit. */
export type PortHit = { key: string; end: WireEnd; nodeId: string };

/** A hit on a placed waypoint: which wire, and its index in that wire's
 * waypoint list. */
export type WaypointHit = { key: string; index: number };

/** A grabbed wire segment: the two anchor indices bounding it, and the
 * axis it slides along (perpendicular to the run itself). */
export type SegmentGrab = { key: string; a: number; b: number; axis: "x" | "y" };

/** Grab radii, in WORLD px. Ports match /live's PORT_HIT_R; a waypoint is
 * a little larger because it has no card edge to aim at, and the wire
 * body is tighter so grabbing a wire never steals a port's click. */
export const PORT_HIT_R = 10;
export const WAYPOINT_HIT_R = 11;
export const WIRE_HIT_R = 7;

/** A waypoint enters the router as a zero-size slot, which is what lets
 * routeOrthogonal stay the ONLY thing that builds a polyline: a point is
 * just a card with no width. */
function pointSlot(p: Point): Slot {
  return { x: p.x, y: p.y, w: 0, h: 0 };
}

/** Closest point on segment a->b to (x,y), plus that distance. */
function nearestOnSegment(a: Point, b: Point, x: number, y: number): { point: Point; dist: number } {
  const dx = b.x - a.x, dy = b.y - a.y;
  const lenSq = dx * dx + dy * dy;
  const t = lenSq === 0 ? 0 : Math.max(0, Math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / lenSq));
  const point = { x: a.x + dx * t, y: a.y + dy * t };
  return { point, dist: Math.hypot(x - point.x, y - point.y) };
}

/** Closest point on a whole polyline — the real distance-to-segment, not
 * an endpoints approximation, so clicking anywhere along a long run of
 * wire actually lands. */
export function nearestOnPolyline(pts: Point[], x: number, y: number): { point: Point; dist: number } {
  let best = { point: pts[0] ?? { x, y }, dist: Infinity };
  for (let i = 1; i < pts.length; i++) {
    const hit = nearestOnSegment(pts[i - 1], pts[i], x, y);
    if (hit.dist < best.dist) best = hit;
  }
  return best;
}

export type LegOpts = {
  fromPort?: WirePort;
  toPort?: WirePort;
  obstacles?: Slot[];
  lane?: number;
};

/** One routed polyline per anchor-to-anchor hop: [from, ...waypoints, to].
 * Kept SEPARATE rather than pre-joined because hit-testing needs to know
 * WHICH hop a click landed on — that index is where a new waypoint gets
 * inserted.
 *
 * Once the owner has placed a waypoint, obstacle avoidance stops applying
 * to that wire: they have said where the path goes, and a router that
 * "helpfully" routes around a node would be overriding them. */
export function routeLegs(
  from: Slot, to: Slot, waypoints: Point[], opts: LegOpts,
): Point[][] {
  if (!waypoints.length) return [routeOrthogonal(from, to, opts)];
  const anchors: Slot[] = [from, ...waypoints.map(pointSlot), to];
  const legs: Point[][] = [];
  for (let i = 1; i < anchors.length; i++) {
    legs.push(routeOrthogonal(anchors[i - 1], anchors[i], {
      fromPort: i === 1 ? opts.fromPort : undefined,
      toPort: i === anchors.length - 1 ? opts.toPort : undefined,
      lane: opts.lane,
    }));
  }
  return legs;
}

/** Smallest jog an orthogonal path is allowed to keep, in world px.
 * Anything tighter reads as a rendering glitch rather than a deliberate
 * bend, so it gets snapped flat. */
export const SIMPLIFY_EPS = 12;

function pathKey(pts: Point[]): string {
  return pts.map((p) => `${p.x},${p.y}`).join("|");
}

/** Drops exact duplicates, then any point whose neighbours share its rail
 * (three points on one line need only two). */
function dropCollinear(pts: Point[]): Point[] {
  const out: Point[] = [];
  for (const p of pts) {
    const prev = out[out.length - 1];
    if (prev && prev.x === p.x && prev.y === p.y) continue;
    out.push({ ...p });
  }
  for (let i = 1; i < out.length - 1;) {
    const a = out[i - 1], b = out[i], c = out[i + 1];
    if ((a.x === b.x && b.x === c.x) || (a.y === b.y && b.y === c.y)) out.splice(i, 1);
    else i++;
  }
  return out;
}

/** Pulls two near-aligned points onto ONE rail. The two ENDPOINTS never
 * move — they are the docked ports — so an interior point always yields
 * to an endpoint.
 *
 * Between two interior points the LATER one yields to the earlier: the
 * rail propagates forward. Meeting in the middle instead looks fairer but
 * mints a brand-new coordinate every pass, so the path never reaches a
 * fixed point and re-simplifying a saved path keeps nudging it. Snapping
 * to an existing rail is idempotent, which is what makes a persisted
 * path reload exactly as it was drawn. */
function alignAxis(pts: Point[], i: number, j: number, axis: "x" | "y"): void {
  const last = pts.length - 1;
  if (i === 0 && j === last) return;
  if (j === last) pts[i][axis] = pts[j][axis];
  else pts[j][axis] = pts[i][axis];
}

function snapRails(pts: Point[], eps: number): Point[] {
  const out = pts.map((p) => ({ ...p }));
  for (let i = 0; i < out.length - 1; i++) {
    const dx = Math.abs(out[i].x - out[i + 1].x);
    const dy = Math.abs(out[i].y - out[i + 1].y);
    if (dx > 0 && dx < eps) alignAxis(out, i, i + 1, "x");
    if (dy > 0 && dy < eps) alignAxis(out, i, i + 1, "y");
  }
  return out;
}

/** Turns a routed polyline into the path a person would have drawn.
 *
 * Routing per hop and concatenating is what mints staircases: every hop
 * contributes its own stub-and-jog, so a couple of bends can stack a
 * dozen 3px steps inside a 150px span (owner, ticket 53cc9bcc: "the lines
 * are all kinda messed up"). Snapping sub-eps jogs onto one rail and then
 * merging the collinear runs collapses each hop to a clean L or Z.
 *
 * POST-PROCESSING, never a second router — routeOrthogonal still produces
 * every corner; this only removes the ones that carry no meaning. The two
 * passes feed each other (a snap creates collinearity, a merge exposes
 * the next near-alignment), so it iterates to a fixed point.
 */
export function simplifyPath(pts: Point[], eps = SIMPLIFY_EPS): Point[] {
  if (pts.length < 3) return pts.map((p) => ({ ...p }));
  let out = pts.map((p) => ({ ...p }));
  for (let pass = 0; pass < 4; pass++) {
    const before = pathKey(out);
    out = dropCollinear(snapRails(out, eps));
    if (pathKey(out) === before) break;
  }
  return out;
}

/** Flattens routeLegs into the single polyline the renderer strokes and
 * packets ride, dropping the duplicate point where two legs meet. */
export function joinLegs(legs: Point[][]): Point[] {
  const out: Point[] = [];
  for (const leg of legs) {
    for (const p of leg) {
      const last = out[out.length - 1];
      if (!last || last.x !== p.x || last.y !== p.y) out.push(p);
    }
  }
  return out;
}

/** Per-project manual wire state: which wire is selected, where each end
 * is docked, and any waypoints bending the middle. Pure client state —
 * the same category as a dragged node position, never a backend entity. */
export class WireEditor {
  /** wireKey of the selected wire, or null. */
  selected: string | null = null;
  private ports = new Map<string, WirePort>();
  private waypoints = new Map<string, Point[]>();

  private portKey(key: string, end: WireEnd): string {
    return `${key}:${end}`;
  }

  portFor(key: string, end: WireEnd, fallback: WirePort): WirePort {
    return this.ports.get(this.portKey(key, end)) ?? fallback;
  }

  /** True once either end has been re-docked by hand — the signal that a
   * wire has left its automatic routing and must honor the owner. */
  hasPort(key: string): boolean {
    return this.ports.has(this.portKey(key, "from")) || this.ports.has(this.portKey(key, "to"));
  }

  /** Re-docks one end anywhere on `slot`'s perimeter. The side and offset
   * come from live/wires.ts's portFromWorld — "drag it anywhere on the
   * card" resolves the same way it does on the live board. */
  setPortFromWorld(key: string, end: WireEnd, slot: Slot, wx: number, wy: number): void {
    this.ports.set(this.portKey(key, end), portFromWorld(slot, wx, wy));
  }

  waypointsFor(key: string): Point[] {
    return this.waypoints.get(key) ?? [];
  }

  /** Inserts a bend on hop `leg` (the index routeLegs assigned), so the
   * new point lands between the two anchors the owner actually clicked
   * between rather than at the end of the list. */
  insertWaypoint(key: string, leg: number, at: Point): void {
    const list = [...this.waypointsFor(key)];
    list.splice(Math.max(0, Math.min(list.length, leg)), 0, at);
    this.waypoints.set(key, list);
  }

  moveWaypoint(key: string, index: number, x: number, y: number): void {
    const list = [...this.waypointsFor(key)];
    if (index < 0 || index >= list.length) return;
    list[index] = { x, y };
    this.waypoints.set(key, list);
  }

  removeWaypoint(key: string, index: number): void {
    const list = [...this.waypointsFor(key)];
    if (index < 0 || index >= list.length) return;
    list.splice(index, 1);
    if (list.length) this.waypoints.set(key, list);
    else this.waypoints.delete(key);
  }

  /** The "drag it back onto the line" gesture: a waypoint dropped on the
   * straight run between its own neighbours is no longer bending
   * anything, so it removes itself instead of lingering as an invisible
   * handle. Returns true if it was dropped. */
  pruneIfStraightened(key: string, index: number, from: Point, to: Point): boolean {
    const list = this.waypointsFor(key);
    const wp = list[index];
    if (!wp) return false;
    const prev = index > 0 ? list[index - 1] : from;
    const next = index < list.length - 1 ? list[index + 1] : to;
    if (nearestOnSegment(prev, next, wp.x, wp.y).dist > WAYPOINT_HIT_R) return false;
    this.removeWaypoint(key, index);
    return true;
  }

  /** Turns the wire's CURRENTLY DRAWN path into explicit bends and returns
   * the two that bound segment `seg`, so dragging that run moves real
   * anchors.
   *
   * This is what makes "just drag the wire" work with no setup (owner:
   * "I can't drag the wire at all", and stop making them "micromanage the
   * need for the double-click anchors") — the anchors the new shape needs
   * are minted here rather than placed by hand first.
   *
   * Materializing is EXACT, not approximate: routeOrthogonal between two
   * corners that already share a rail collapses straight back to that run
   * once simplifyPath merges it, so the path does not jump the moment a
   * drag begins.
   *
   * The two endpoints can't move — they are docked ports — so a segment
   * touching one gets a duplicate of that endpoint as its movable anchor.
   */
  grabSegment(key: string, pts: Point[], seg: number): SegmentGrab | null {
    if (pts.length < 2 || seg < 0 || seg > pts.length - 2) return null;
    const A = pts[seg], B = pts[seg + 1];
    // Perpendicular by construction: a vertical run slides along x, a
    // horizontal run slides along y.
    const axis: "x" | "y" = Math.abs(A.x - B.x) <= Math.abs(A.y - B.y) ? "x" : "y";
    const chain = pts.map((p) => ({ ...p }));
    let a = seg, b = seg + 1;
    if (b === chain.length - 1) chain.splice(b, 0, { ...chain[b] });
    if (a === 0) { chain.splice(1, 0, { ...chain[0] }); a = 1; b += 1; }
    this.waypoints.set(key, chain.slice(1, -1));
    return { key, a: a - 1, b: b - 1, axis };
  }

  /** Slides a grabbed segment perpendicular to its own axis by moving both
   * of its anchors onto one new rail. */
  moveSegment(grab: SegmentGrab, value: number): void {
    const list = [...this.waypointsFor(grab.key)];
    if (!list[grab.a] || !list[grab.b]) return;
    const onRail = (p: Point): Point =>
      grab.axis === "x" ? { x: value, y: p.y } : { x: p.x, y: value };
    list[grab.a] = onRail(list[grab.a]);
    list[grab.b] = onRail(list[grab.b]);
    this.waypoints.set(grab.key, list);
  }

  /** Snaps STORED bends onto shared rails with their neighbours, so a
   * saved path is already clean. Without this a drag would persist its
   * raw pointer coordinates and reload as the exact staircase the
   * renderer had just simplified away — the bend would look right until
   * you refreshed. */
  simplifyWaypoints(key: string, from: Point, to: Point, eps = SIMPLIFY_EPS): void {
    const list = this.waypointsFor(key);
    if (!list.length) return;
    const chain = [from, ...list.map((p) => ({ ...p })), to];
    for (let i = 1; i < chain.length - 1; i++) {
      for (const neighbour of [chain[i - 1], chain[i + 1]]) {
        if (Math.abs(chain[i].x - neighbour.x) < eps) chain[i].x = neighbour.x;
        if (Math.abs(chain[i].y - neighbour.y) < eps) chain[i].y = neighbour.y;
      }
    }
    // An anchor a drag has made redundant RETIRES ITSELF — one that landed
    // on a neighbour, or that no longer bends anything. Snapping alone
    // would leave it in the list: invisible in the path, but still drawn
    // as a handle (stacked on the port dot when it collapsed onto an end)
    // and accumulating with every drag. dropCollinear never removes the
    // two endpoints, so a coincident anchor yields to them, not the
    // reverse.
    const kept = dropCollinear(chain).slice(1, -1);
    if (kept.length) this.waypoints.set(key, kept);
    else this.waypoints.delete(key);
  }

  /** "reset layout": drop every manual wire edit. Positions are cleared
   * by WorkflowGraph in the same breath. */
  clear(): void {
    this.selected = null;
    this.ports.clear();
    this.waypoints.clear();
  }

  /** The port store itself. Exposed ONLY so a consumer that already had
   * a public port map of its own — graphState.ts's `portOverrides`, which
   * LivePage and the /live suites both name — can keep that name pointing
   * at THIS one store instead of keeping a second map beside it. Two
   * stores is how the boards drifted the first time. */
  get portMap(): Map<string, WirePort> {
    return this.ports;
  }

  /** True once this wire carries ANY manual edit — a re-docked end or a
   * placed bend. The signal that it has left automatic routing. */
  hasEdits(key: string): boolean {
    return this.hasPort(key) || this.waypointsFor(key).length > 0;
  }

  serializePorts(): Record<string, WirePort> {
    return Object.fromEntries(this.ports);
  }

  serializeWaypoints(): Record<string, Point[]> {
    return Object.fromEntries(this.waypoints);
  }

  /** Rehydrates saved ports, dropping anything malformed or belonging to
   * a wire this board no longer has — the same drop-unknown-keys rule
   * graphState.ts's hydratePortOverrides uses, so stale state can never
   * accumulate forever. */
  hydratePorts(saved: Record<string, WirePort>, knownKeys: Set<string>): void {
    const sides = ["left", "right", "top", "bottom"];
    for (const [k, port] of Object.entries(saved ?? {})) {
      if (!port || !sides.includes(port.side) || !Number.isFinite(port.t)) continue;
      const end: WireEnd | null = k.endsWith(":from") ? "from" : k.endsWith(":to") ? "to" : null;
      if (!end) continue;
      if (!knownKeys.has(k.slice(0, k.length - (end.length + 1)))) continue;
      this.ports.set(k, { side: port.side, t: port.t });
    }
  }

  hydrateWaypoints(saved: Record<string, Point[]>, knownKeys: Set<string>): void {
    for (const [key, list] of Object.entries(saved ?? {})) {
      if (!knownKeys.has(key) || !Array.isArray(list)) continue;
      const clean = list.filter((p) => p && Number.isFinite(p.x) && Number.isFinite(p.y));
      if (clean.length) this.waypoints.set(key, clean.map((p) => ({ x: p.x, y: p.y })));
    }
  }
}

// ---------------------------------------------------------------------------
// The canvas-agnostic interaction layer (task be7a5d2d). Everything above
// is per-wire STATE; everything below binds that state to a board without
// knowing which board it is.
// ---------------------------------------------------------------------------

/** What a canvas must answer about its own wires for the shared layer to
 * route and hit-test them. Deliberately tiny: four questions, none of
 * them about editing. If a surface ever needs a fifth, check first that
 * it is really a fact about the board and not a behaviour that belongs
 * here — a behaviour answered per surface is a dialect, and two dialects
 * is what this module exists to end. */
export interface WireSurface {
  /** Every wire key on the board right now. */
  keys(): string[];
  /** The two node slots a wire spans, in world space, with the ids of
   * the nodes those ends are docked to. null when either end is gone. */
  ends(key: string): { from: Slot; to: Slot; fromId: string; toId: string } | null;
  /** Rectangles this wire's route should try to clear. */
  obstacles(key: string): Slot[];
  /** True while this wire should keep the plain edge-midpoint elbow it
   * has always drawn rather than the port-aware path. /workflows says
   * yes for its untouched FSM chain; /live is always port-aware. Only
   * consulted while the wire is unedited. */
  prefersPlainElbow(key: string): boolean;
  /** Whether an UNBENT route gets the simplify pass.
   *
   * /workflows: true — that is its behaviour today.
   *
   * /live: FALSE, and this is the whole anti-misfire. SIMPLIFY_EPS is 12
   * and wires.ts's LANE_STEP is 10, so snapRails would pull a one-lane
   * obstacle-avoidance jog flat — on a board that feeds every other card
   * in as an obstacle. An unbent live wire must come back from the
   * router untouched. Once a wire HAS bends the question is moot: the
   * owner has said where it goes, so obstacle avoidance is already
   * dropped and there is nothing left to flatten. */
  readonly simplifyUnbentRoutes: boolean;
}

/** One canvas's wire editing: the shared WireEditor state plus every
 * hit-test and geometry call a page needs to drive it. A board holds one
 * of these and delegates; it never re-implements a method. */
export class WireInteraction {
  readonly editor = new WireEditor();

  constructor(private readonly surface: WireSurface) {}

  get selected(): string | null {
    return this.editor.selected;
  }

  set selected(key: string | null) {
    this.editor.selected = key;
  }

  /** One routed polyline per anchor-to-anchor hop. Drawing joins them;
   * hit-testing needs them apart, because WHICH hop a click landed on is
   * where a new waypoint gets inserted. */
  legs(key: string): Point[][] {
    const ends = this.surface.ends(key);
    if (!ends) return [];
    const waypoints = this.editor.waypointsFor(key);
    if (this.surface.prefersPlainElbow(key) && !this.editor.hasEdits(key)) {
      return routeLegs(ends.from, ends.to, [], {});
    }
    const lane = laneFor(key);
    return routeLegs(ends.from, ends.to, waypoints, {
      fromPort: this.editor.portFor(key, "from", autoPort(ends.from, ends.to, lane)),
      toPort: this.editor.portFor(key, "to", autoPort(ends.to, ends.from, lane)),
      // Obstacle avoidance is dropped once waypoints exist: the owner has
      // said where the path goes, and re-routing around a node would be
      // overriding them.
      obstacles: waypoints.length ? undefined : this.surface.obstacles(key),
      lane,
    });
  }

  /** The wire's current polyline, re-derived every frame so a dragged
   * node drags its wires along for free.
   *
   * An UNBENT wire on a surface that has opted out of simplification is
   * handed back exactly as routeOrthogonal built it — not joined, not
   * simplified. joinLegs would drop degenerate duplicate points and
   * simplifyPath would snap sub-eps jogs onto one rail, and on /live
   * those jogs are the lane offsets carrying a wire around a card. This
   * one branch is why promoting the editor does not change a single
   * pixel of the live board until somebody bends something. */
  route(key: string): Point[] {
    const legs = this.legs(key);
    if (!legs.length) return [];
    if (!this.editor.waypointsFor(key).length && !this.surface.simplifyUnbentRoutes) {
      return legs[0];
    }
    // Simplify AFTER joining: chaining a route per hop is what produces
    // the staircases, so the cleanup has to see the whole path at once.
    return simplifyPath(joinLegs(legs));
  }

  /** Settles a wire's stored bends onto clean rails. Called when a drag
   * ends and before persisting, so a saved path never reloads as the
   * staircase the renderer had already smoothed over. */
  settle(key: string): void {
    const pts = this.route(key);
    if (pts.length >= 2) this.editor.simplifyWaypoints(key, pts[0], pts[pts.length - 1]);
  }

  /** The wire whose stroke passes nearest a world point — a real
   * distance-to-segment test, so anywhere along a long run is grabbable,
   * not just near an endpoint. */
  wireAtWorld(wx: number, wy: number): string | null {
    let best: string | null = null;
    let bestDist = WIRE_HIT_R;
    for (const key of this.surface.keys()) {
      const pts = this.route(key);
      if (pts.length < 2) continue;
      const hit = nearestOnPolyline(pts, wx, wy);
      if (hit.dist < bestDist) { bestDist = hit.dist; best = key; }
    }
    return best;
  }

  /** Which hop of a wire a point lands on, and exactly where — the
   * insertion index and position for a new waypoint. */
  legAtWorld(key: string, wx: number, wy: number): { leg: number; point: Point } | null {
    let best: { leg: number; point: Point } | null = null;
    let bestDist = WIRE_HIT_R * 2;
    this.legs(key).forEach((pts, i) => {
      if (pts.length < 2) return;
      const hit = nearestOnPolyline(pts, wx, wy);
      if (hit.dist < bestDist) { bestDist = hit.dist; best = { leg: i, point: hit.point }; }
    });
    return best;
  }

  /** Endpoint dots, hit-tested against the polyline the RENDERER
   * strokes rather than re-derived from side/offset math (mx-0a0bf4: the
   * hit-test must MIRROR the draw). Reading route() is what makes this
   * correct for a plain-elbow wire too, whose drawn ends are edge
   * midpoints and not ports at all.
   *
   * Every drawn dot is grabbable, on both boards. Both canvases paint a
   * dot on EVERY wire, so scoping the grab to the selected one would
   * mean drawing a handle that does nothing — the same mirror rule,
   * pointed the other way. */
  portAtWorld(wx: number, wy: number): PortHit | null {
    let best: PortHit | null = null;
    let bestDist = PORT_HIT_R;
    for (const key of this.surface.keys()) {
      const ends = this.surface.ends(key);
      const pts = this.route(key);
      if (!ends || pts.length < 2) continue;
      const candidates: { end: WireEnd; at: Point; nodeId: string }[] = [
        { end: "from", at: pts[0], nodeId: ends.fromId },
        { end: "to", at: pts[pts.length - 1], nodeId: ends.toId },
      ];
      for (const c of candidates) {
        const d = Math.hypot(wx - c.at.x, wy - c.at.y);
        if (d < bestDist) { bestDist = d; best = { key, end: c.end, nodeId: c.nodeId }; }
      }
    }
    return best;
  }

  /** Placed waypoints of the SELECTED wire only — handles are drawn for
   * the selected wire alone, so those are the only ones to hit-test. */
  waypointAtWorld(wx: number, wy: number): WaypointHit | null {
    const key = this.editor.selected;
    if (!key) return null;
    let best: WaypointHit | null = null;
    let bestDist = WAYPOINT_HIT_R;
    this.editor.waypointsFor(key).forEach((p, index) => {
      const d = Math.hypot(wx - p.x, wy - p.y);
      if (d < bestDist) { bestDist = d; best = { key, index }; }
    });
    return best;
  }

  /** Index of the polyline run under a world point — which segment a
   * body drag has grabbed. */
  segmentAtWorld(key: string, wx: number, wy: number): number | null {
    const pts = this.route(key);
    let best: number | null = null;
    let bestDist = WIRE_HIT_R;
    for (let i = 1; i < pts.length; i++) {
      const hit = nearestOnPolyline([pts[i - 1], pts[i]], wx, wy);
      if (hit.dist < bestDist) { bestDist = hit.dist; best = i - 1; }
    }
    return best;
  }

  /** Starts a body drag: resolves the run under the cursor and
   * materializes the anchors it needs. Null when the point isn't on the
   * wire. */
  beginSegmentDrag(key: string, wx: number, wy: number): SegmentGrab | null {
    const seg = this.segmentAtWorld(key, wx, wy);
    if (seg === null) return null;
    return this.editor.grabSegment(key, this.route(key), seg);
  }

  /** World-space slot a wire end is docked to — what a port drag
   * re-docks against. */
  slotForEnd(key: string, end: WireEnd): Slot | undefined {
    const ends = this.surface.ends(key);
    if (!ends) return undefined;
    return end === "from" ? ends.from : ends.to;
  }

  /** "reset layout": drop selection, re-docked ends and every bend. */
  clear(): void {
    this.editor.clear();
  }

  serialize(): { ports: Record<string, WirePort>; waypoints: Record<string, Point[]> } {
    return { ports: this.editor.serializePorts(), waypoints: this.editor.serializeWaypoints() };
  }

  /** Rehydrates saved edits, dropping anything belonging to a wire this
   * board no longer has. */
  hydrate(ports?: Record<string, WirePort>, waypoints?: Record<string, Point[]>): void {
    const known = new Set(this.surface.keys());
    if (ports) this.editor.hydratePorts(ports, known);
    if (waypoints) this.editor.hydrateWaypoints(waypoints, known);
  }
}

// ---------------------------------------------------------------------------
// Rendering. ONE function knows what an editable wire looks like, so the
// two boards cannot drift into two oranges the way they drifted into two
// gesture sets.
// ---------------------------------------------------------------------------

export type EditableWirePaint = {
  pts: Point[];
  kind: WireKind;
  /** Carrying real flow right now — the wire's own live/idle tint. */
  live: boolean;
  selected: boolean;
  /** Bend handles, drawn for the selected wire only. */
  waypoints?: Point[];
};

/** Strokes one wire, its two endpoint dots and (when selected) its bend
 * handles.
 *
 * A selected wire wears the LIVE stroke so it is unmistakable against the
 * dim idle runs around it. Owner (ticket 53cc9bcc): an active wire is
 * orange END TO END — body, ports and handles one color. Orange only at
 * the handles was the half-signal that got rejected, and the colour is
 * the single PALETTE.selection token, never a second literal. */
export function drawEditableWire(ctx: CanvasRenderingContext2D, w: EditableWirePaint): void {
  if (w.pts.length < 2) return;
  const selColor = w.selected ? PALETTE.selection : undefined;
  const hot = w.live || w.selected;
  drawWire(ctx, w.pts, w.kind, hot, selColor);
  // Each endpoint gets one port dot in the wire's own stroke color, drawn
  // under the cards. A selected wire's handles switch to the selection
  // hue, marking the two dots that are draggable right now.
  const portColor = selColor ?? wireColor(w.kind, w.live);
  drawPort(ctx, w.pts[0], portColor, hot);
  drawPort(ctx, w.pts[w.pts.length - 1], portColor, hot);
  if (w.selected) drawWaypointHandles(ctx, w.waypoints ?? []);
}

/** The draggable bend handles on the selected wire. Hollow squares in the
 * selection hue so they read as grab targets, never as packets (which are
 * near-white filled squares — one shape, one meaning). */
function drawWaypointHandles(ctx: CanvasRenderingContext2D, waypoints: Point[]): void {
  const r = 5;
  ctx.save();
  ctx.strokeStyle = PALETTE.selection;
  ctx.fillStyle = PALETTE.ground;
  ctx.lineWidth = 2;
  for (const p of waypoints) {
    ctx.beginPath();
    ctx.rect(p.x - r, p.y - r, r * 2, r * 2);
    ctx.fill();
    ctx.stroke();
  }
  ctx.restore();
}
