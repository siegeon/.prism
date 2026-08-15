/** Owns the /live graph's mutable state: node/edge/packet bookkeeping and
 * how each WorkEvent mutates it. No physics (see layout.ts) — this module
 * is the single owner of "what exists and where it's animating to/from",
 * kept separate from cards.ts/wires.ts/hud.ts/idle.ts (pure rendering) so
 * the visuals can be iterated without touching state, and vice versa. */

import type { GraphEdge, GraphNode, GraphSnapshot, WorkEvent } from "./types";
import { LayoutEngine, type Slot } from "./layout";
import { routeOrthogonal, type Point } from "./wires";
import { spawnPacket, stepPackets, type Packet } from "./packets";

const SPAWN_MS = 550;
const PULSE_MS = 700;
const GHOST_MS = 650;
/** A step row's capacity bar reads as "fresh" right after a heartbeat and
 * decays to empty over this window — the directive's "else heartbeat
 * recency" fallback for when no real in-step progress is wired yet. */
const HEARTBEAT_DECAY_MS = 20_000;

/** Round 2, piece 5 (load as length): a task/subtask's Tokens buffer bar
 * fills a real amount on every tokens.turn that targets it and drains
 * continuously, so its LENGTH is recent-throughput -- almost always
 * moving while work flows, visibly emptying the moment it stops (never
 * a static icon, the round1 critic's exact complaint). Fill scales with
 * tok_s (a hotter session fills more per arrival); drain is a fixed
 * leak, so the bar settles near an equilibrium proportional to
 * throughput rather than just ratcheting to 1 and sitting there. */
const BUFFER_FILL_MIN = 0.06;
const BUFFER_FILL_MAX = 0.4;
const BUFFER_FILL_TOK_S_SPAN = 1200;
const BUFFER_DRAIN_PER_MS = 1 / 5000;

function bufferFillFor(tokS: number): number {
  const frac = Math.max(0, Math.min(1, (tokS || 0) / BUFFER_FILL_TOK_S_SPAN));
  return BUFFER_FILL_MIN + frac * (BUFFER_FILL_MAX - BUFFER_FILL_MIN);
}

/** Round 2, piece 2 (motion on the edges): a wire only carries a marker
 * every SPAWN_COOLDOWN_MS at most per edge ("SPARSE... cap ~1 marker per
 * wire per 1.2s"), and a structural (parent_of) wire only tints teal
 * while flow has propagated up it within FLOW_TINT_WINDOW_MS -- both
 * windows are real-event-driven (set on tokens.turn), never a timer. */
const SPAWN_COOLDOWN_MS = 1200;
const FLOW_TINT_WINDOW_MS = 4000;
const MIN_FLOW_TOK_S = 3;

export type LiveNode = GraphNode & {
  parentTaskId: string | null; // subtask -> its root task id
  driverOfId: string | null; // session -> the task/subtask id it's driven_in to
  x: number; y: number; // current animated (drawn) position, canvas coords
  slot: Slot; // resting target position/size from LayoutEngine
  spawnAt: number; // performance.now() at creation, for slide-in easing
  spawnFromX: number; spawnFromY: number;
  pulseUntil: number;
  tokensGhostUntil: number;
  stepGhostUntil: number;
  lastHeartbeatAt: number;
  selected: boolean;
  /** Recent-throughput buffer bar fraction (0..1), task/subtask cards
   * only -- see the BUFFER_FILL_ and BUFFER_DRAIN_PER_MS constants above. */
  bufferFrac: number;
};

export type LiveEdge = { source: string; target: string; kind: GraphEdge["kind"] };

function easeOutCubic(t: number): number {
  const c = Math.max(0, Math.min(1, t));
  return 1 - Math.pow(1 - c, 3);
}

export class GraphState {
  nodes: LiveNode[] = [];
  edges: LiveEdge[] = [];
  packets: Packet[] = [];
  layout = new LayoutEngine();
  width = 800;
  height = 600;
  /** Pan/zoom camera — screen = (world - pan) * zoom. Owned here so a
   * click hit-test and the renderer agree on the same transform. */
  pan = { x: 0, y: 0 };
  zoom = 1;
  booted = false;

  private byId = new Map<string, LiveNode>();
  /** tok/s samples for the HUD sparkline: {t: performance.now(), v: total tok/s}. */
  tokSHistory: { t: number; v: number }[] = [];
  /** $ spend samples for the HUD's spend-rate meter, same shape/window. */
  spendHistory: { t: number; v: number }[] = [];
  /** Per-edge (by `${source}->${target}`) last-flow timestamp, for the
   * structural wire's "flowing recently" tint (piece 2) and to gate the
   * HUD/card honesty around what's actually moving. */
  private edgeFlowAt = new Map<string, number>();
  /** Per-edge last packet-spawn timestamp -- SPAWN_COOLDOWN_MS floor so
   * a hot wire still reads as SPARSE motion, not a packet train. */
  private edgeLastSpawnAt = new Map<string, number>();

  private makeNode(
    n: GraphNode, parentTaskId: string | null, driverOfId: string | null,
    slot: Slot, now: number, fromX: number, fromY: number,
  ): LiveNode {
    return {
      ...n, parentTaskId, driverOfId,
      x: fromX, y: fromY, slot,
      spawnAt: now, spawnFromX: fromX, spawnFromY: fromY,
      pulseUntil: 0, tokensGhostUntil: 0, stepGhostUntil: 0,
      lastHeartbeatAt: 0, selected: false, bufferFrac: 0,
    };
  }

  bootstrap(snapshot: GraphSnapshot, width: number, height: number): void {
    this.width = width;
    this.height = height;
    this.layout.reset();
    this.byId.clear();
    this.nodes = [];
    this.edges = snapshot.edges.map((e) => ({ ...e }));
    this.packets = [];
    this.pan = { x: 0, y: 0 };
    this.zoom = 1;

    const taskCount = snapshot.nodes.filter((n) => n.kind === "task").length;
    this.layout.setDensity(taskCount);

    const parentOf = new Map<string, string>(); // subtask -> task
    const driverTarget = new Map<string, string>(); // session -> driven task/subtask
    for (const e of snapshot.edges) {
      if (e.kind === "parent_of") parentOf.set(e.target, e.source);
      if (e.kind === "driven_in") driverTarget.set(e.source, e.target);
    }

    const now = performance.now();
    // Place tasks first, then subtasks (need parent slot), then sessions.
    const byKind = (k: string) => snapshot.nodes.filter((n) => n.kind === k);
    for (const n of byKind("task")) {
      const slot = this.layout.placeTask(n.id);
      const node = this.makeNode(n, null, null, slot, now, slot.x, slot.y);
      this.nodes.push(node);
      this.byId.set(n.id, node);
    }
    for (const n of byKind("subtask")) {
      const parentId = parentOf.get(n.id) ?? "";
      const slot = this.layout.placeSubtask(n.id, parentId);
      const parentSlot = this.layout.slotFor(parentId);
      const node = this.makeNode(
        n, parentId || null, null, slot, now,
        parentSlot?.x ?? slot.x, parentSlot?.y ?? slot.y);
      this.nodes.push(node);
      this.byId.set(n.id, node);
    }
    for (const n of byKind("session")) {
      const driverId = driverTarget.get(n.id) ?? "";
      const slot = this.layout.placeSession(n.id, driverId);
      const driverSlot = this.layout.slotFor(driverId);
      const node = this.makeNode(
        n, null, driverId || null, slot, now,
        driverSlot?.x ?? slot.x, driverSlot?.y ?? slot.y);
      this.nodes.push(node);
      this.byId.set(n.id, node);
    }
    this.booted = true;
  }

  resize(width: number, height: number): void {
    this.width = width;
    this.height = height;
  }

  private wireEndpointsFor(edge: LiveEdge): Point[] | null {
    const a = this.byId.get(edge.source);
    const b = this.byId.get(edge.target);
    if (!a || !b) return null;
    // routeOrthogonal wants (from, to) as SLOTS in the direction the wire
    // visually flows: structure edges parent(task)->child(subtask) flow
    // left->right; token edges session->task flow session(below) up into
    // the task, which routeOrthogonal's fallback branch already handles.
    return routeOrthogonal(
      { x: a.x, y: a.y, w: a.slot.w, h: a.slot.h },
      { x: b.x, y: b.y, w: b.slot.w, h: b.slot.h },
    );
  }

  private upsertSessionNode(id: string, driverId: string, now: number): LiveNode {
    const existing = this.byId.get(id);
    if (existing) return existing;
    const slot = this.layout.placeSession(id, driverId);
    const driverSlot = this.layout.slotFor(driverId);
    const node = this.makeNode(
      {
        id, kind: "session", label: id.slice(0, 8), status: "",
        workflow_step: "", gate_state: "", activity_state: "active",
        heartbeat_age_s: null, tok_s: null, tokens_total: null,
        href: `/sessions/${id}`,
      },
      null, driverId, slot, now, driverSlot?.x ?? slot.x, driverSlot?.y ?? slot.y,
    );
    this.nodes.push(node);
    this.byId.set(id, node);
    return node;
  }

  /** Lazily creates a minimal placeholder card for a task/subtask id
   * first referenced by a WorkEvent that arrives AFTER the page's boot
   * snapshot (e.g. a subtask spawned mid-session). Root-cause fix for
   * the piece-4 cross-check finding ("HUD ticked while every card read
   * Tokens: 0"): every event handler used to `byId.get(task_id)` and
   * silently drop the event on a miss -- nothing ever added a NEW
   * task/subtask node (only upsertSessionNode had lazy-create), so a
   * task created after boot stayed invisible forever, however many
   * tokens.turn/heartbeat events named it; the HUD (which only sums
   * session nodes it was ALSO never allowed to create for that task)
   * would then diverge from that task's own still-absent-or-frozen
   * card. Placeholder starts as a bare task-shaped card; the next
   * task.changed backfills title/status/step once one arrives. */
  private ensureTaskNode(id: string, now: number): LiveNode {
    const existing = this.byId.get(id);
    if (existing) return existing;
    const slot = this.layout.placeTask(id);
    const node = this.makeNode(
      {
        id, kind: "task", label: id.slice(0, 8), status: "in_progress",
        workflow_step: "", gate_state: "none", activity_state: "",
        heartbeat_age_s: null, tok_s: null, tokens_total: null,
        href: `/tasks/${id}`,
      },
      null, null, slot, now, slot.x, slot.y,
    );
    this.nodes.push(node);
    this.byId.set(id, node);
    return node;
  }

  private ensureEdge(source: string, target: string, kind: GraphEdge["kind"]): void {
    const exists = this.edges.some(
      (e) => e.source === source && e.target === target && e.kind === kind);
    if (!exists) this.edges.push({ source, target, kind });
  }

  private edgeKey(source: string, target: string): string {
    return `${source}->${target}`;
  }

  /** Has this exact edge (by source/target) carried real token flow
   * within the last FLOW_TINT_WINDOW_MS? Read by draw.ts to decide
   * whether a structural (parent_of) wire tints teal. */
  isEdgeFlowing(source: string, target: string, now: number): boolean {
    const at = this.edgeFlowAt.get(this.edgeKey(source, target));
    return at !== undefined && now - at < FLOW_TINT_WINDOW_MS;
  }

  /** Spawns at most one packet per edgeKey per SPAWN_COOLDOWN_MS -- the
   * grammar's "SPARSE... one visible marker per 300-600px" translated
   * into a cadence cap rather than a density cap. */
  private maybeSpawnPacket(edgeKey: string, pts: Point[] | null, now: number): void {
    if (!pts) return;
    const last = this.edgeLastSpawnAt.get(edgeKey);
    if (last !== undefined && now - last < SPAWN_COOLDOWN_MS) return;
    this.edgeLastSpawnAt.set(edgeKey, now);
    this.packets.push(spawnPacket(edgeKey, pts));
  }

  applyEvent(event: WorkEvent): void {
    const now = performance.now();
    if (event.type === "task.changed") {
      const node = this.ensureTaskNode(event.task_id, now);
      const fields = event.fields ?? {};
      if (typeof fields.status === "string") node.status = fields.status;
      if (typeof fields.workflow_step === "string" && fields.workflow_step !== node.workflow_step) {
        node.workflow_step = fields.workflow_step;
        node.stepGhostUntil = now + GHOST_MS;
      }
      if (typeof fields.gate_state === "string") node.gate_state = fields.gate_state;
      if (typeof fields.title === "string" && fields.title) node.label = fields.title;
      return;
    }
    if (event.type === "drive.heartbeat") {
      const node = this.ensureTaskNode(event.task_id, now);
      node.pulseUntil = now + PULSE_MS;
      node.lastHeartbeatAt = now;
      if (event.step && event.step !== node.workflow_step) {
        node.workflow_step = event.step;
        node.stepGhostUntil = now + GHOST_MS;
      }
      return;
    }
    if (event.type === "agent.run") {
      this.ensureTaskNode(event.task_id, now);
      if (event.session_id) {
        const sessionNode = this.upsertSessionNode(event.session_id, event.task_id, now);
        this.ensureEdge(event.session_id, event.task_id, "driven_in");
        sessionNode.pulseUntil = now + PULSE_MS;
      }
      return;
    }
    if (event.type === "tokens.turn") {
      const taskNode = this.ensureTaskNode(event.task_id, now);
      const sessionNode = this.upsertSessionNode(event.session_id, event.task_id, now);
      sessionNode.tok_s = event.tok_s;
      sessionNode.tokens_total = event.tokens_total;
      sessionNode.tokensGhostUntil = now + GHOST_MS;
      // Both the ghosted NUMBER and the buffer bar update off the same
      // event, in the same synchronous call -- the piece-4 fix: a card
      // can never show a stale number next to a fresh bar or vice versa.
      taskNode.tokensGhostUntil = now + GHOST_MS;
      taskNode.bufferFrac = Math.min(1, taskNode.bufferFrac + bufferFillFor(event.tok_s));
      this.ensureEdge(event.session_id, event.task_id, "driven_in");

      const hasFlow = event.tok_s > MIN_FLOW_TOK_S;
      const directEdge = this.edges.find(
        (e) => e.source === event.session_id && e.target === event.task_id && e.kind === "driven_in");
      if (directEdge && hasFlow) {
        const directKey = this.edgeKey(directEdge.source, directEdge.target);
        this.edgeFlowAt.set(directKey, now);
        this.maybeSpawnPacket(directKey, this.wireEndpointsFor(directEdge), now);
      }

      // Propagate up the structural wire: a subtask's token motion is
      // ALSO visible flowing into its parent task's own wire (design
      // directive: "token motion on a subtask propagates a marker up
      // the subtask->parent structural wire too (aggregate flow reads
      // upward)") -- this is the round1 critic's single biggest gap,
      // "nearly all the visible wire pixels in the graph never show
      // color, a marker, or motion".
      if (taskNode.parentTaskId && hasFlow) {
        const parentEdge = this.edges.find(
          (e) => e.kind === "parent_of" && e.source === taskNode.parentTaskId && e.target === taskNode.id);
        if (parentEdge) {
          const structKey = this.edgeKey(parentEdge.source, parentEdge.target);
          this.edgeFlowAt.set(structKey, now);
          const downPts = this.wireEndpointsFor(parentEdge);
          const upPts = downPts ? [...downPts].reverse() : null;
          this.maybeSpawnPacket(structKey, upPts, now);
        }
      }

      this.recordTokSample(now);
      return;
    }
  }

  /** Sum of live tok_s / spend across nodes, sampled for the HUD's
   * sparkline and spend meter (last ~5 minutes kept). */
  private recordTokSample(now: number): void {
    let tokTotal = 0;
    let spendTotal = 0;
    for (const n of this.nodes) {
      if (n.kind === "session") tokTotal += n.tok_s || 0;
      else spendTotal += n.spend_usd || 0;
    }
    this.tokSHistory.push({ t: now, v: tokTotal });
    this.spendHistory.push({ t: now, v: spendTotal });
    const cutoff = now - 5 * 60_000;
    while (this.tokSHistory.length && this.tokSHistory[0].t < cutoff) this.tokSHistory.shift();
    while (this.spendHistory.length && this.spendHistory[0].t < cutoff) this.spendHistory.shift();
  }

  /** Advance spawn-in easing + packet travel + buffer-bar drain. Called
   * once per rAF frame. */
  step(dtMs: number, now: number): void {
    for (const n of this.nodes) {
      const elapsed = now - n.spawnAt;
      if (elapsed >= SPAWN_MS) {
        n.x = n.slot.x;
        n.y = n.slot.y;
      } else {
        const e = easeOutCubic(elapsed / SPAWN_MS);
        n.x = n.spawnFromX + (n.slot.x - n.spawnFromX) * e;
        n.y = n.spawnFromY + (n.slot.y - n.spawnFromY) * e;
      }
      if (n.kind !== "session" && n.bufferFrac > 0) {
        n.bufferFrac = Math.max(0, n.bufferFrac - BUFFER_DRAIN_PER_MS * dtMs);
      }
    }
    this.packets = stepPackets(this.packets, dtMs);
  }

  /** Screen -> world, inverting the pan/zoom camera. */
  toWorld(screenX: number, screenY: number): { x: number; y: number } {
    return { x: screenX / this.zoom + this.pan.x, y: screenY / this.zoom + this.pan.y };
  }

  nodeAtWorld(x: number, y: number): LiveNode | null {
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const n = this.nodes[i];
      if (x >= n.x && x <= n.x + n.slot.w && y >= n.y && y <= n.y + n.slot.h) return n;
    }
    return null;
  }

  select(id: string | null): void {
    for (const n of this.nodes) n.selected = n.id === id;
  }

  destroy(): void {
    this.nodes = [];
    this.edges = [];
    this.packets = [];
  }
}

export { HEARTBEAT_DECAY_MS };
