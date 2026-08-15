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

  private makeNode(
    n: GraphNode, parentTaskId: string | null, driverOfId: string | null,
    slot: Slot, now: number, fromX: number, fromY: number,
  ): LiveNode {
    return {
      ...n, parentTaskId, driverOfId,
      x: fromX, y: fromY, slot,
      spawnAt: now, spawnFromX: fromX, spawnFromY: fromY,
      pulseUntil: 0, tokensGhostUntil: 0, stepGhostUntil: 0,
      lastHeartbeatAt: 0, selected: false,
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

  private ensureEdge(source: string, target: string, kind: GraphEdge["kind"]): void {
    const exists = this.edges.some(
      (e) => e.source === source && e.target === target && e.kind === kind);
    if (!exists) this.edges.push({ source, target, kind });
  }

  applyEvent(event: WorkEvent): void {
    const now = performance.now();
    if (event.type === "task.changed") {
      const node = this.byId.get(event.task_id);
      if (!node) return;
      const fields = event.fields ?? {};
      if (typeof fields.status === "string") node.status = fields.status;
      if (typeof fields.workflow_step === "string" && fields.workflow_step !== node.workflow_step) {
        node.workflow_step = fields.workflow_step;
        node.stepGhostUntil = now + GHOST_MS;
      }
      if (typeof fields.gate_state === "string") node.gate_state = fields.gate_state;
      return;
    }
    if (event.type === "drive.heartbeat") {
      const node = this.byId.get(event.task_id);
      if (!node) return;
      node.pulseUntil = now + PULSE_MS;
      node.lastHeartbeatAt = now;
      if (event.step && event.step !== node.workflow_step) {
        node.workflow_step = event.step;
        node.stepGhostUntil = now + GHOST_MS;
      }
      return;
    }
    if (event.type === "agent.run") {
      if (!this.byId.get(event.task_id)) return;
      if (event.session_id) {
        const sessionNode = this.upsertSessionNode(event.session_id, event.task_id, now);
        this.ensureEdge(event.session_id, event.task_id, "driven_in");
        sessionNode.pulseUntil = now + PULSE_MS;
      }
      return;
    }
    if (event.type === "tokens.turn") {
      const taskNode = this.byId.get(event.task_id);
      if (!taskNode) return;
      const sessionNode = this.upsertSessionNode(event.session_id, event.task_id, now);
      sessionNode.tok_s = event.tok_s;
      sessionNode.tokens_total = event.tokens_total;
      sessionNode.tokensGhostUntil = now + GHOST_MS;
      taskNode.tokensGhostUntil = now + GHOST_MS;
      this.ensureEdge(event.session_id, event.task_id, "driven_in");
      const edge = this.edges.find(
        (e) => e.source === event.session_id && e.target === event.task_id && e.kind === "driven_in");
      if (edge) {
        const pts = this.wireEndpointsFor(edge);
        if (pts) this.packets.push(spawnPacket(`${edge.source}->${edge.target}`, pts, event.tok_s));
      }
      this.recordTokSample(now);
      return;
    }
  }

  /** Sum of live tok_s across all session nodes, sampled for the HUD's
   * sparkline (last ~5 minutes kept). */
  private recordTokSample(now: number): void {
    const total = this.nodes
      .filter((n) => n.kind === "session")
      .reduce((sum, n) => sum + (n.tok_s || 0), 0);
    this.tokSHistory.push({ t: now, v: total });
    const cutoff = now - 5 * 60_000;
    while (this.tokSHistory.length && this.tokSHistory[0].t < cutoff) this.tokSHistory.shift();
  }

  /** Advance spawn-in easing + packet travel. Called once per rAF frame. */
  step(dtMs: number, now: number): void {
    for (const n of this.nodes) {
      const elapsed = now - n.spawnAt;
      if (elapsed >= SPAWN_MS) {
        n.x = n.slot.x;
        n.y = n.slot.y;
        continue;
      }
      const e = easeOutCubic(elapsed / SPAWN_MS);
      n.x = n.spawnFromX + (n.slot.x - n.spawnFromX) * e;
      n.y = n.spawnFromY + (n.slot.y - n.spawnFromY) * e;
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
