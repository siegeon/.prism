/** Owns the /live graph's mutable state: the d3-force simulation, the
 * node/edge/packet arrays, and how each WorkEvent mutates them. Kept
 * separate from draw.ts (pure rendering) so the visuals can be iterated
 * on without touching the physics/event-application logic, and vice
 * versa (task instruction: "structure it so visuals are easy to
 * iterate — one module owning draw/state"). */

import {
  forceSimulation, forceManyBody, forceLink, forceCenter, forceCollide,
  type Simulation, type SimulationNodeDatum,
} from "d3-force";
import type { GraphEdge, GraphNode, GraphSnapshot, WorkEvent } from "./types";

export type SimNode = GraphNode & SimulationNodeDatum & {
  /** Timestamp (performance.now()-scale, ms) until which the node's ring
   * should render pulsing — set on drive.heartbeat, cleared by decay. */
  pulseUntil: number;
};

export type SimLink = {
  source: SimNode;
  target: SimNode;
  kind: GraphEdge["kind"];
};

export type Packet = {
  link: SimLink;
  /** 0..1 progress along the edge, source -> target. */
  t: number;
  /** Progress per millisecond — derived from tok_s so a hot session's
   * dots visibly move faster/more often than an idle one. */
  speedPerMs: number;
};

const PULSE_MS = 700;
/** tok_s below this floors to the slowest packet speed so a trickle of
 * tokens still produces a visibly-moving dot instead of a stalled one. */
const MIN_TOK_S = 5;
const MAX_TOK_S = 400;
const MIN_SPEED_PER_MS = 1 / 4000;   // crosses the edge in 4s
const MAX_SPEED_PER_MS = 1 / 600;    // crosses the edge in 0.6s

function speedForTokS(tokS: number): number {
  const clamped = Math.max(MIN_TOK_S, Math.min(MAX_TOK_S, tokS));
  const frac = (clamped - MIN_TOK_S) / (MAX_TOK_S - MIN_TOK_S);
  return MIN_SPEED_PER_MS + frac * (MAX_SPEED_PER_MS - MIN_SPEED_PER_MS);
}

export class GraphState {
  nodes: SimNode[] = [];
  links: SimLink[] = [];
  packets: Packet[] = [];
  sim: Simulation<SimNode, SimLink> | null = null;
  width = 800;
  height = 600;

  private byId = new Map<string, SimNode>();

  bootstrap(snapshot: GraphSnapshot, width: number, height: number): void {
    this.width = width;
    this.height = height;
    this.nodes = snapshot.nodes.map((n) => ({
      ...n,
      x: width / 2 + (Math.random() - 0.5) * 120,
      y: height / 2 + (Math.random() - 0.5) * 120,
      pulseUntil: 0,
    }));
    this.byId = new Map(this.nodes.map((n) => [n.id, n]));
    this.links = snapshot.edges
      .map((e) => {
        const source = this.byId.get(e.source);
        const target = this.byId.get(e.target);
        if (!source || !target) return null;
        return { source, target, kind: e.kind } as SimLink;
      })
      .filter((l): l is SimLink => l !== null);
    this.packets = [];

    this.sim?.stop();
    this.sim = forceSimulation(this.nodes)
      .force("charge", forceManyBody().strength(-220))
      .force("link", forceLink<SimNode, SimLink>(this.links).id((d) => d.id).distance(110).strength(0.5))
      .force("center", forceCenter(width / 2, height / 2))
      .force("collide", forceCollide<SimNode>().radius(radiusFor).strength(0.9))
      .alpha(1)
      .alphaDecay(0.02);
  }

  resize(width: number, height: number): void {
    this.width = width;
    this.height = height;
    this.sim?.force("center", forceCenter(width / 2, height / 2));
    this.sim?.alpha(0.3).restart();
  }

  private upsertSessionNode(id: string, label: string): SimNode {
    const existing = this.byId.get(id);
    if (existing) return existing;
    const anchor = this.nodes[0];
    const node: SimNode = {
      id, kind: "session", label, status: "", workflow_step: "",
      gate_state: "", activity_state: "active", heartbeat_age_s: null,
      tok_s: null, tokens_total: null, href: `/sessions/${id}`,
      x: (anchor?.x ?? this.width / 2) + (Math.random() - 0.5) * 40,
      y: (anchor?.y ?? this.height / 2) + (Math.random() - 0.5) * 40,
      pulseUntil: 0,
    };
    this.nodes.push(node);
    this.byId.set(id, node);
    return node;
  }

  private ensureLink(sourceId: string, targetId: string, kind: GraphEdge["kind"]): SimLink | null {
    const source = this.byId.get(sourceId);
    const target = this.byId.get(targetId);
    if (!source || !target) return null;
    const existing = this.links.find(
      (l) => l.source.id === sourceId && l.target.id === targetId && l.kind === kind);
    if (existing) return existing;
    const link: SimLink = { source, target, kind };
    this.links.push(link);
    return link;
  }

  private restartSim(alpha = 0.4): void {
    if (!this.sim) return;
    this.sim.nodes(this.nodes);
    this.sim.force("link", forceLink<SimNode, SimLink>(this.links).id((d) => d.id).distance(110).strength(0.5));
    this.sim.alpha(alpha).restart();
  }

  /** Mutate state from one pushed WorkEvent. Returns true when the node
   * set changed (so the caller knows the simulation needs a restart —
   * already handled internally, but useful for callers that also want
   * to know). */
  applyEvent(event: WorkEvent): void {
    const now = performance.now();
    if (event.type === "task.changed") {
      const node = this.byId.get(event.task_id);
      if (!node) return;
      const fields = event.fields ?? {};
      if (typeof fields.status === "string") node.status = fields.status;
      if (typeof fields.workflow_step === "string") node.workflow_step = fields.workflow_step;
      if (typeof fields.gate_state === "string") node.gate_state = fields.gate_state;
      return;
    }
    if (event.type === "drive.heartbeat") {
      const node = this.byId.get(event.task_id);
      if (!node) return;
      node.pulseUntil = now + PULSE_MS;
      if (event.step) node.workflow_step = event.step;
      return;
    }
    if (event.type === "agent.run") {
      const taskNode = this.byId.get(event.task_id);
      if (!taskNode) return;
      if (event.session_id) {
        const sessionNode = this.upsertSessionNode(event.session_id, event.session_id.slice(0, 8));
        this.ensureLink(event.session_id, event.task_id, "driven_in");
        sessionNode.pulseUntil = now + PULSE_MS;
        this.restartSim(0.35);
      }
      return;
    }
    if (event.type === "tokens.turn") {
      const taskNode = this.byId.get(event.task_id);
      const sessionNode = this.byId.get(event.session_id) ?? this.upsertSessionNode(
        event.session_id, event.session_id.slice(0, 8));
      if (!taskNode) return;
      sessionNode.tok_s = event.tok_s;
      sessionNode.tokens_total = event.tokens_total;
      const link = this.ensureLink(event.session_id, event.task_id, "driven_in") ?? this.links.find(
        (l) => l.source.id === event.session_id && l.target.id === event.task_id);
      if (!link) return;
      this.restartSim(0.15);
      this.packets.push({ link, t: 0, speedPerMs: speedForTokS(event.tok_s || MIN_TOK_S) });
      return;
    }
  }

  /** Advance packet travel + drop finished ones. Called once per rAF
   * frame from LivePage.tsx with the elapsed ms since the previous frame. */
  step(dtMs: number): void {
    if (this.packets.length) {
      for (const p of this.packets) p.t += p.speedPerMs * dtMs;
      this.packets = this.packets.filter((p) => p.t < 1);
    }
  }

  nodeAt(x: number, y: number): SimNode | null {
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const n = this.nodes[i];
      const dx = (n.x ?? 0) - x;
      const dy = (n.y ?? 0) - y;
      if (dx * dx + dy * dy <= radiusFor(n) * radiusFor(n) * 2.25) return n;
    }
    return null;
  }

  destroy(): void {
    this.sim?.stop();
    this.sim = null;
  }
}

export function radiusFor(n: Pick<SimNode, "kind">): number {
  if (n.kind === "task") return 18;
  if (n.kind === "subtask") return 12;
  return 7; // session
}
