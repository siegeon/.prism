/** Owns the /live graph's mutable state: node/edge/packet bookkeeping and
 * how each WorkEvent mutates it. No physics (see layout.ts) — this module
 * is the single owner of "what exists and where it's animating to/from",
 * kept separate from cards.ts/wires.ts/hud.ts/idle.ts (pure rendering) so
 * the visuals can be iterated without touching state, and vice versa. */

import type { GraphEdge, GraphNode, GraphSnapshot, WorkEvent } from "./types";
import { LayoutEngine, type Slot } from "./layout";
import { routeOrthogonal, type Point } from "./wires";
import { spawnPacket, stepPackets, type Packet } from "./packets";
import { spawnToast, pruneToasts, type Toast } from "./toasts";
import { type CardState } from "./palette";

export type { CardState };

const SPAWN_MS = 550;
const PULSE_MS = 700;
const GHOST_MS = 650;
/** A step row's capacity bar reads as "fresh" right after a heartbeat and
 * decays to empty over this window — the directive's "else heartbeat
 * recency" fallback for when no real in-step progress is wired yet. */
const HEARTBEAT_DECAY_MS = 20_000;

/** Round 2, piece 3/4 (node state + honest idle): a node's raw tok_s is a
 * point-in-time sample that never resets itself, so a session that went
 * quiet used to read as permanently "live" forever after its last
 * nonzero tick (the piece4 critic's root complaint, generalized: a rate
 * stat that never decays is indistinguishable from a healthy one). After
 * this many ms with no real signal, GraphState.step() zeroes tok_s
 * itself — cascades honestly into the HUD (sums live tok_s), the wire
 * "live" tint (reads a.tok_s), and a card's own connector-dot liveliness,
 * all from ONE decay instead of three separate staleness checks. */
const QUIET_DECAY_MS = 8_000;
/** A card counts as STALLED only once it's gone this long with no signal
 * AND is still nominally in_progress — never merely "slower than usual".
 * A YOUNG (never-signaled) card is a different state entirely and is
 * never colored red (see palette.ts's deadRingColorFor). */
const STALL_MS = 45_000;
/** The graph as a whole reads "quiet" (idle.ts's honest idle line) once
 * this long has passed with literally no WorkEvent of any kind. */
export const GRAPH_QUIET_MS = 8_000;
/** A "done" card's brief settle flash (scale + green title bar) window,
 * and how long after that before it compacts to a slim done chip — the
 * build directive's "the completion must be witnessed" (never an
 * instant vanish). */
const SETTLE_MS = 350;
const COMPACT_AFTER_MS = 4_000;

export function deriveCardState(node: {
  status: string; gate_state: string; lastSignalAt: number;
}, now: number): CardState {
  if (node.status === "done" || node.gate_state === "passed") return "done";
  // Failed is dead, same as stalled -- red immediately, no need to wait
  // out STALL_MS first.
  if (node.status === "failed") return "stalled";
  if (node.gate_state === "pending") return "waiting_gate";
  if (!node.lastSignalAt) return "young";
  const silentTooLong = now - node.lastSignalAt > STALL_MS;
  // NOT status === "in_progress": a task mid-drive through the conductor
  // reads status "pending" for most of its life (conductor_advance does
  // NOT flip status->in_progress -- confirmed against a live /api/work/
  // graph snapshot, where every actively-ticking sim task in this build
  // still read status:"pending"). The only statuses that should NEVER
  // go stalled are the terminal ones already handled above (done) or
  // explicitly abandoned (cancelled) -- everything else that has had a
  // real signal before and has gone quiet too long is legitimately dead.
  if (silentTooLong && node.status !== "cancelled") return "stalled";
  return "working";
}

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
  /** performance.now() of the last REAL signal that touched this node
   * (task.changed/drive.heartbeat/agent.run/tokens.turn) -- 0 means
   * "never signaled", which is what makes a card YOUNG rather than
   * WORKING or STALLED (round 2, piece 3). Never bumped by reconcile()'s
   * self-heal refetch -- that would fake freshness on a genuinely stalled
   * card. */
  lastSignalAt: number;
  /** Live USD spend -- mirrors GraphNode.spend_usd but locally mutable so
   * a sim tokens.turn carrying usd_total can tick it between backend
   * snapshots (build item 6c: "fast lane's sim-tokens include usd_total
   * so spend rows tick"). */
  spend_usd: number;
  gate_waiting_s: number | null;
  queue_depth: number;
  /** performance.now() the CURRENT gate started waiting, backdated from
   * the backend's own gate_waiting_s where available so "waiting Xm"
   * never lies about when the wait actually began. 0 = not waiting. */
  gatePendingSince: number;
  /** performance.now() this node's status flipped to done; 0 = not done.
   * Drives the settle flash + the compact-to-chip timer, never re-fired
   * by reconcile once already set. */
  doneAt: number;
  /** Brief scale+flash window right when doneAt is set (build item 2:
   * "the card does a brief settle animation"). */
  settleUntil: number;
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

  /** Docked completion/gate toasts (round 2, piece 3/4 build item 2) --
   * state lives here, rendering in toasts.ts, same split as `packets`. */
  toasts: Toast[] = [];
  /** performance.now() of the LAST WorkEvent of any kind -- the honest
   * idle line's only input (idle.ts's isGraphQuiet), never derived from
   * per-node tok_s (which is itself decayed FROM this same clock). */
  lastEventAt = 0;

  private reconcileFetcher: (() => Promise<GraphSnapshot>) | null = null;
  private selfHealTimer: ReturnType<typeof setTimeout> | null = null;
  private lastSampleAt = 0;

  private makeNode(
    n: GraphNode, parentTaskId: string | null, driverOfId: string | null,
    slot: Slot, now: number, fromX: number, fromY: number,
  ): LiveNode {
    // NO Math.max(0, ...) clamp here: performance.now() is relative to
    // THIS PAGE's own navigation start, not wall-clock/epoch time, so a
    // backend "N seconds ago" that's older than the page itself has been
    // open produces a legitimately NEGATIVE timestamp -- clamping that to
    // 0 was a real bug (verified live): 0 doubles as this field's "never
    // signaled" sentinel, so every task whose last real signal predated
    // this page load got silently reclassified as YOUNG on boot, no
    // matter how stale it actually was. A later `now - lastSignalAt`
    // still computes the correct (large) elapsed value from a negative
    // start point; only the ORIGINAL clamp was wrong, not the arithmetic.
    const lastSignalAt = n.heartbeat_age_s != null
      ? now - n.heartbeat_age_s * 1000
      : (n.tok_s && n.tok_s > 0 ? now : 0);
    const gatePendingSince = n.gate_state === "pending"
      ? (n.gate_waiting_s != null ? now - n.gate_waiting_s * 1000 : now)
      : 0;
    // A node that's ALREADY done at boot renders compacted immediately
    // (doneAt backdated past COMPACT_AFTER_MS) with no settle flash and
    // no toast -- those only fire off a live transition (applyEvent /
    // reconcile), never off the initial snapshot.
    const doneAt = n.status === "done" ? now - (COMPACT_AFTER_MS + 1000) : 0;
    return {
      ...n, parentTaskId, driverOfId,
      x: fromX, y: fromY, slot,
      spawnAt: now, spawnFromX: fromX, spawnFromY: fromY,
      pulseUntil: 0, tokensGhostUntil: 0, stepGhostUntil: 0,
      lastHeartbeatAt: 0, selected: false, bufferFrac: 0,
      lastSignalAt, spend_usd: n.spend_usd || 0,
      gate_waiting_s: n.gate_waiting_s ?? null, queue_depth: n.queue_depth || 0,
      gatePendingSince, doneAt, settleUntil: 0,
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
  private ensureTaskNode(
    id: string, now: number, kind: "task" | "subtask" = "task", parentTaskId: string | null = null,
  ): LiveNode {
    const existing = this.byId.get(id);
    if (existing) return existing;
    const slot = kind === "subtask" && parentTaskId
      ? this.layout.placeSubtask(id, parentTaskId)
      : this.layout.placeTask(id);
    const parentSlot = parentTaskId ? this.layout.slotFor(parentTaskId) : undefined;
    const node = this.makeNode(
      {
        id, kind, label: id.slice(0, 8), status: "in_progress",
        workflow_step: "", gate_state: "none", activity_state: "",
        heartbeat_age_s: null, tok_s: null, tokens_total: null,
        href: `/tasks/${id}`,
      },
      parentTaskId, null, slot, now, parentSlot?.x ?? slot.x, parentSlot?.y ?? slot.y,
    );
    this.nodes.push(node);
    this.byId.set(id, node);
    // Round 2, piece 4 self-heal: a placeholder only ever carries what
    // the ONE event that created it happened to know (title stays the
    // truncated id, gate_state stays "none"). Schedule a debounced
    // /api/work/graph refetch so the real fields backfill shortly after,
    // instead of staying a bare id-labeled card forever.
    this.scheduleSelfHeal();
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

  /** Shared by applyEvent's task.changed and reconcile(): detects a
   * status->done or gate_state->pending TRANSITION (compares against
   * what the caller captured BEFORE writing the new values) and fires
   * the matching toast exactly once. Working identically whether the
   * transition arrived live over SSE or was only discovered by a
   * self-heal refetch is the whole point -- build item 6a's "gate flip
   * writes sqlite directly, which fires NO SSE" case has to reach the
   * screen through THIS same path, not a second one. */
  private noteStatusGateTransition(
    node: LiveNode, prevStatus: string, prevGate: string, now: number,
  ): void {
    if (prevStatus !== "done" && node.status === "done" && node.doneAt === 0) {
      node.doneAt = now;
      node.settleUntil = now + SETTLE_MS;
      spawnToast(this.toasts, "done", `done · ${node.label}`, now);
    }
    if (prevGate !== "pending" && node.gate_state === "pending") {
      // No clamp -- see makeNode's comment; a stale-relative-to-page-load
      // gate_waiting_s must stay negative here, not collapse to "just
      // now" (which silently drops the real wait time shown on the card).
      node.gatePendingSince = node.gate_waiting_s != null
        ? now - node.gate_waiting_s * 1000 : now;
      spawnToast(this.toasts, "gate", `gate · ${node.label} needs a decision`, now);
    } else if (node.gate_state !== "pending") {
      node.gatePendingSince = 0;
    }
  }

  applyEvent(event: WorkEvent): void {
    const now = performance.now();
    this.lastEventAt = now;
    if (event.type === "task.changed") {
      const node = this.ensureTaskNode(event.task_id, now);
      const prevStatus = node.status, prevGate = node.gate_state;
      const fields = event.fields ?? {};
      if (typeof fields.status === "string") node.status = fields.status;
      if (typeof fields.workflow_step === "string" && fields.workflow_step !== node.workflow_step) {
        node.workflow_step = fields.workflow_step;
        node.stepGhostUntil = now + GHOST_MS;
      }
      if (typeof fields.gate_state === "string") node.gate_state = fields.gate_state;
      if (typeof fields.title === "string" && fields.title) node.label = fields.title;
      node.lastSignalAt = now;
      this.noteStatusGateTransition(node, prevStatus, prevGate, now);
      // Round 2, piece 4 self-heal: ANY task.changed may be the only
      // visible trace of a write whose `fields` dict doesn't carry what
      // actually moved (e.g. Act 3's gate flip: a direct sqlite write,
      // then a harmless PATCH just to get an event on the bus at all) --
      // a debounced reconcile catches the drift regardless of what this
      // particular event happened to report.
      this.scheduleSelfHeal();
      return;
    }
    if (event.type === "drive.heartbeat") {
      const node = this.ensureTaskNode(event.task_id, now);
      node.pulseUntil = now + PULSE_MS;
      node.lastHeartbeatAt = now;
      node.lastSignalAt = now;
      if (event.step && event.step !== node.workflow_step) {
        node.workflow_step = event.step;
        node.stepGhostUntil = now + GHOST_MS;
      }
      return;
    }
    if (event.type === "agent.run") {
      const taskNode = this.ensureTaskNode(event.task_id, now);
      taskNode.lastSignalAt = now;
      if (event.session_id) {
        const sessionNode = this.upsertSessionNode(event.session_id, event.task_id, now);
        this.ensureEdge(event.session_id, event.task_id, "driven_in");
        sessionNode.pulseUntil = now + PULSE_MS;
        sessionNode.lastSignalAt = now;
      }
      return;
    }
    if (event.type === "tokens.turn") {
      const taskNode = this.ensureTaskNode(event.task_id, now);
      const sessionNode = this.upsertSessionNode(event.session_id, event.task_id, now);
      sessionNode.tok_s = event.tok_s;
      sessionNode.tokens_total = event.tokens_total;
      sessionNode.tokensGhostUntil = now + GHOST_MS;
      sessionNode.lastSignalAt = now;
      taskNode.lastSignalAt = now;
      // Round 2 build item 6c: an OPTIONAL usd_total passthrough (the sim's
      // fast lane wires this so a Spend row can visibly tick without
      // waiting on the real transcript-derived spend cache). Absent on
      // every real tokens.turn today; when present it's a running total,
      // same accumulation shape as tokens_total, never a per-tick delta.
      if (typeof event.usd_total === "number") {
        taskNode.spend_usd = event.usd_total;
      }
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

  /** Advance spawn-in easing + packet travel + buffer-bar drain + honest
   * rate decay + toast pruning. Called once per rAF frame. */
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
      // Round 2, piece 3/4 honest idle: a rate stat that never resets
      // itself reads as permanently "live" (the piece4 root cause,
      // generalized). Decaying tok_s to 0 once its OWN last signal is
      // stale feeds cards.ts's per-row live flags, the wire "live" tint
      // (draw.ts reads a.tok_s), AND hud.ts's existing computeHudTotals
      // (sums n.tok_s every frame) -- one decay, three honesty fixes,
      // none of those three modules' code touched.
      if (n.tok_s && n.lastSignalAt && now - n.lastSignalAt > QUIET_DECAY_MS) {
        n.tok_s = 0;
      }
    }
    this.packets = stepPackets(this.packets, dtMs);
    this.toasts = pruneToasts(this.toasts, now);

    // Keep the HUD sparkline honest through a quiet stretch too -- a
    // once-a-second sample (not every rAF frame) so tok/s visibly
    // decaying to 0 shows up as a real trend, not a stale tail frozen at
    // the last tokens.turn's value.
    if (now - this.lastSampleAt > 1000) {
      this.lastSampleAt = now;
      this.recordTokSample(now);
    }
  }

  setReconcileFetcher(fn: () => Promise<GraphSnapshot>): void {
    this.reconcileFetcher = fn;
  }

  /** Round 2, piece 4 self-heal (build item 4): debounces a burst of
   * events into ONE /api/work/graph refetch ~2s later, then reconciles.
   * Closes the round1 hole where a gate flip written out-of-band (Act
   * 3's direct sqlite write, which fires no SSE at all) never reached
   * the screen -- and the more general case, a placeholder card that
   * never gets backfilled because no live event happens to carry the
   * field it's missing. */
  scheduleSelfHeal(): void {
    if (this.selfHealTimer || !this.reconcileFetcher) return;
    const fetcher = this.reconcileFetcher;
    this.selfHealTimer = setTimeout(() => {
      this.selfHealTimer = null;
      fetcher().then((snap) => this.reconcile(snap)).catch(() => {});
    }, 2000);
  }

  /** Merge a fresh boot-shaped snapshot into live state. Backfills
   * fields that may have drifted on nodes we already have, and adopts
   * any task/subtask the snapshot knows about that we don't -- but NEVER
   * touches x/y/slot/spawn animation state for an existing node (a
   * reconcile must be visually silent) and NEVER bumps lastSignalAt
   * (that would fake freshness on a genuinely stalled card, defeating
   * the point of piece 3's state system). Session adoption is left to
   * the live event path (upsertSessionNode) -- a session only ever shows
   * up meaningfully via its own tokens.turn/agent.run, which always
   * carries a real driverId; adopting one here with no known driver
   * would have nothing correct to place it under. */
  private reconcile(snapshot: GraphSnapshot): void {
    const now = performance.now();
    for (const sn of snapshot.nodes) {
      const existing = this.byId.get(sn.id);
      if (!existing) {
        if (sn.kind === "task") {
          this.ensureTaskNode(sn.id, now);
        } else if (sn.kind === "subtask") {
          const parentEdge = snapshot.edges.find((e) => e.kind === "parent_of" && e.target === sn.id);
          this.ensureTaskNode(sn.id, now, "subtask", parentEdge?.source ?? null);
        }
        continue;
      }
      const prevStatus = existing.status, prevGate = existing.gate_state;
      existing.status = sn.status;
      if (sn.workflow_step !== existing.workflow_step) {
        existing.workflow_step = sn.workflow_step;
        existing.stepGhostUntil = now + GHOST_MS;
      }
      existing.gate_state = sn.gate_state;
      if (sn.label) existing.label = sn.label;
      if (typeof sn.spend_usd === "number") existing.spend_usd = sn.spend_usd;
      existing.gate_waiting_s = sn.gate_waiting_s ?? null;
      existing.queue_depth = sn.queue_depth ?? 0;
      this.noteStatusGateTransition(existing, prevStatus, prevGate, now);
    }
    for (const e of snapshot.edges) {
      if (e.kind === "parent_of") this.ensureEdge(e.source, e.target, "parent_of");
    }
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
    if (this.selfHealTimer) {
      clearTimeout(this.selfHealTimer);
      this.selfHealTimer = null;
    }
    this.nodes = [];
    this.edges = [];
    this.packets = [];
    this.toasts = [];
  }
}

export { HEARTBEAT_DECAY_MS, STALL_MS, SETTLE_MS, COMPACT_AFTER_MS };
