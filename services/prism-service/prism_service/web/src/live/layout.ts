/** Deterministic auto-layout for the /live graph — NO physics, no
 * force-directed jitter (design directive: "auto-LAYOUT, not physics").
 * Root task cards form a left column; subtask cards fan right of their
 * parent; session/agent cards attach below-right of whatever they drive.
 * A node is assigned a slot ONCE, on first sight, and keeps it forever —
 * that's what makes existing nodes "never wander" while new ones can
 * still slide in (graphState.ts owns the slide-in animation itself,
 * using the slot this module hands back as the animation target). */

export type Slot = { x: number; y: number; w: number; h: number };

export const BASE_CARD_W = 236;
// +12 each (round 2, piece 5) to fit the new Tokens-row buffer bar
// without cramping the rows already below it (Step bar, Spend, gate).
export const BASE_TASK_H = 128;
export const BASE_SUB_H = 108;
export const BASE_SESSION_H = 60;
const COL_GAP = 96;
const ROW_GAP = 22;
const ORIGIN_X = 48;
/** Clears the fixed HUD panel (top-left, ~206px tall after round 2's
 * taller instrumentation rebuild — see hud.ts) so the first task card is
 * never born underneath it; the HUD sits in screen space and never
 * pans, so this is the one layout constant that has to know about it. */
const ORIGIN_Y = 228;

/** Above this many top-level task cards, shrink everything so 1-15 tasks
 * still fit without cards overlapping (directive: "overflow -> smaller
 * cards or clusters"). Picked once per bootstrap from the snapshot size. */
function densityScale(taskCount: number): number {
  if (taskCount <= 6) return 1;
  if (taskCount <= 10) return 0.82;
  return 0.62;
}

export class LayoutEngine {
  private scale = 1;
  private taskOrder: string[] = [];
  private taskSlot = new Map<string, Slot>();
  private subOrder = new Map<string, string[]>(); // parentTaskId -> subtask ids
  private subSlot = new Map<string, Slot>();
  private sessOrder = new Map<string, string[]>(); // driverId -> session ids
  private sessSlot = new Map<string, Slot>();

  setDensity(taskCount: number): void {
    this.scale = densityScale(taskCount);
  }

  slotFor(id: string): Slot | undefined {
    return this.taskSlot.get(id) ?? this.subSlot.get(id) ?? this.sessSlot.get(id);
  }

  cardSize(kind: "task" | "subtask" | "session"): { w: number; h: number } {
    const h = kind === "task" ? BASE_TASK_H : kind === "subtask" ? BASE_SUB_H : BASE_SESSION_H;
    return { w: BASE_CARD_W * this.scale, h: h * this.scale };
  }

  /** Vertical gap between a driver card's bottom edge and its session
   * card's top edge. Round 2 builder-B residual (piece 4 critic gap:
   * "session cards sit nearly flush under their driver so the
   * session->task wire is ~15-50px and markers flash sub-200ms") — floor
   * this at 150px, scale-independent, so the wire is always long enough
   * for a sparse in-transit marker (packets.ts's ~140px/s constant
   * speed) to sit visibly mid-span for a beat rather than blink past in
   * under two rAF frames. */
  private sessionDropY(): number {
    return Math.max(150, 170 * this.scale);
  }

  /** Extra vertical room a row must reserve below a task/subtask card so
   * a session card that arrives later (placeSession attaches below-right
   * of whatever it drives) never overlaps the NEXT card in the same
   * column — sessions are event-driven and can show up at any time, but
   * row pitch is committed the moment the row above is placed, so it has
   * to assume worst case up front rather than react after the fact. */
  private sessionReserve(): number {
    const { h } = this.cardSize("session");
    return this.sessionDropY() + h + ROW_GAP * this.scale * 0.3;
  }

  placeTask(id: string): Slot {
    const existing = this.taskSlot.get(id);
    if (existing) return existing;
    const idx = this.taskOrder.length;
    this.taskOrder.push(id);
    const { w, h } = this.cardSize("task");
    const pitch = h + ROW_GAP * this.scale + this.sessionReserve();
    const slot: Slot = { x: ORIGIN_X, y: ORIGIN_Y + idx * pitch, w, h };
    this.taskSlot.set(id, slot);
    return slot;
  }

  placeSubtask(id: string, parentTaskId: string): Slot {
    const existing = this.subSlot.get(id);
    if (existing) return existing;
    const parentSlot = this.taskSlot.get(parentTaskId) ?? this.placeTask(parentTaskId);
    const list = this.subOrder.get(parentTaskId) ?? [];
    const idx = list.length;
    list.push(id);
    this.subOrder.set(parentTaskId, list);
    const { w, h } = this.cardSize("subtask");
    const x = parentSlot.x + parentSlot.w + COL_GAP * this.scale;
    const pitch = h + ROW_GAP * this.scale + this.sessionReserve();
    const y = parentSlot.y + idx * pitch;
    const slot: Slot = { x, y, w, h };
    this.subSlot.set(id, slot);
    return slot;
  }

  placeSession(id: string, driverId: string): Slot {
    const existing = this.sessSlot.get(id);
    if (existing) return existing;
    const driverSlot = this.slotFor(driverId) ?? this.placeTask(driverId);
    const list = this.sessOrder.get(driverId) ?? [];
    const idx = list.length;
    list.push(id);
    this.sessOrder.set(driverId, list);
    const { w, h } = this.cardSize("session");
    const x = driverSlot.x + 18 * this.scale + idx * (w + 14 * this.scale);
    const y = driverSlot.y + driverSlot.h + this.sessionDropY();
    const slot: Slot = { x, y, w, h };
    this.sessSlot.set(id, slot);
    return slot;
  }

  /** Extent of everything placed so far, used to size the pan/zoom "world"
   * and to auto-frame on boot. */
  bounds(): { w: number; h: number } {
    let maxX = ORIGIN_X, maxY = ORIGIN_Y;
    for (const m of [this.taskSlot, this.subSlot, this.sessSlot]) {
      for (const s of m.values()) {
        maxX = Math.max(maxX, s.x + s.w);
        maxY = Math.max(maxY, s.y + s.h);
      }
    }
    return { w: maxX + ORIGIN_X, h: maxY + ORIGIN_Y };
  }

  reset(): void {
    this.taskOrder = [];
    this.taskSlot.clear();
    this.subOrder.clear();
    this.subSlot.clear();
    this.sessOrder.clear();
    this.sessSlot.clear();
  }
}
