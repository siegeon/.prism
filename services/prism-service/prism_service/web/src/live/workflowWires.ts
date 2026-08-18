/** Direct manipulation of a wire on the /workflows canvas — select it,
 * re-dock either endpoint, bend the middle (owner at green_gate review:
 * "I should be able to click on them like draw.io / mxGraph so that I can
 * move this and help ensure their path is the way I want it to be").
 *
 * State + geometry only; workflowGraph.ts owns the nodes and wires and
 * drives the hit-tests, WorkflowsPage.tsx owns the gestures. Split out so
 * workflowGraph.ts does not grow into the god-module /live's graphState.ts
 * already is.
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

import { portFromWorld, routeOrthogonal, type Point, type WirePort } from "./wires";
import type { Slot } from "./layout";

export type WireEnd = "from" | "to";

/** A hit on a wire's endpoint dot: which wire, which end, and the node
 * that end is docked to. Mirrors graphState.ts's PortHit. */
export type PortHit = { key: string; end: WireEnd; nodeId: string };

/** A hit on a placed waypoint: which wire, and its index in that wire's
 * waypoint list. */
export type WaypointHit = { key: string; index: number };

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
    this.waypoints.set(key, chain.slice(1, -1));
  }

  /** "reset layout": drop every manual wire edit. Positions are cleared
   * by WorkflowGraph in the same breath. */
  clear(): void {
    this.selected = null;
    this.ports.clear();
    this.waypoints.clear();
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
