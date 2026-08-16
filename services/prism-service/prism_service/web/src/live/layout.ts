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
// Round 4 item 2b: session cards now also carry a throughput buffer bar
// row (cards.ts no longer excludes `kind === "session"` from it), so the
// card needs room for Title + Tokens row + bar, not just Title + Tokens.
export const BASE_SESSION_H = 82;
const COL_GAP = 96;
const ROW_GAP = 22;
const ORIGIN_X = 48;
/** Round 5 item 2: how many columns a parent's subtasks fan across
 * before wrapping to a new row -- see placeSubtask's doc comment. */
const SUB_COLS = 3;
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

  /** Round 3 item 1 residual fix (found while verifying auto-fit against a
   * real scenario run): a root task with 3+ siblings all fanned out at
   * ONE constant x, stacked purely by y, builds a tall NARROW content
   * bbox (one card wide, N cards tall). GraphState.autoFitCamera fits
   * that bbox correctly, but min(width/contentW, height/contentH) is then
   * dominated by the TALL dimension, so the fitted zoom leaves huge
   * unused margins on both sides of a 1920x1080 viewport -- verified
   * live: auto-fit was mathematically correct and the screen still read
   * as ~80% empty black, reproducing critic 5's original squint
   * complaint through a different mechanism. Fanning subtasks across
   * SUB_COLS(idx) columns roughly SQUARES the bbox's aspect ratio, which
   * is what actually lets auto-fit zoom in far enough to read as "fills
   * the screen" on a landscape viewport.
   *
   * Round 5 item 2 residual (found reading the actual FILM, not a single
   * frame): each ROW's pitch reserves a full session-card's worth of
   * vertical space (sessionReserve(), ~239px) below EVERY row regardless
   * of whether a session ever attaches there -- with a fixed 2 columns,
   * 5 siblings (routine: 2 never-advanced queued children the design
   * directive's queue-glyph slice deliberately seeds, plus 3 real
   * subtasks) fan into 3 rows, and 3x that per-row reserve is what
   * crashed autoFit's zoom from ~0.85 to ~0.46 the instant the 3rd row
   * appeared -- verified live via GraphState's own zoom/pan fields at
   * that exact moment. SUB_COLS bumped 2 -> 3 (a FIXED constant, not
   * computed from the running sibling count) cuts that same 5-sibling
   * fan to 2 rows instead of 3, removing one whole reserve-inflated
   * row's worth of height. Deliberately fixed rather than count-
   * dependent: a column count that changes once MORE siblings arrive
   * would recompute `col`/`row` differently for indices already placed
   * under the old count, silently colliding an EARLIER sibling with a
   * later one at the same slot -- a placed sibling's slot must stay
   * correct forever once assigned (this module's own doc comment, top of
   * file), and only a value that never changes across a parent's whole
   * lifetime can guarantee that. */
  placeSubtask(id: string, parentTaskId: string): Slot {
    const existing = this.subSlot.get(id);
    if (existing) return existing;
    const parentSlot = this.taskSlot.get(parentTaskId) ?? this.placeTask(parentTaskId);
    const list = this.subOrder.get(parentTaskId) ?? [];
    const idx = list.length;
    list.push(id);
    this.subOrder.set(parentTaskId, list);
    const { w, h } = this.cardSize("subtask");
    const col = idx % SUB_COLS;
    const row = Math.floor(idx / SUB_COLS);
    const x = parentSlot.x + parentSlot.w + COL_GAP * this.scale + col * (w + COL_GAP * this.scale * 0.6);
    const pitch = h + ROW_GAP * this.scale + this.sessionReserve();
    const y = parentSlot.y + row * pitch;
    const slot: Slot = { x, y, w, h };
    this.subSlot.set(id, slot);
    return slot;
  }

  /** Shared by placeSession (idx = the session's own fresh order slot)
   * and reslotSessionsOf (idx = an EXISTING session's already-assigned
   * order, recomputed against a driver slot that just moved) -- the one
   * place "below-right of the driver" gets computed, so the two callers
   * can never drift apart on the math. */
  private sessionSlotAt(driverSlot: Slot, idx: number): Slot {
    const { w, h } = this.cardSize("session");
    const x = driverSlot.x + 18 * this.scale + idx * (w + 14 * this.scale);
    const y = driverSlot.y + driverSlot.h + this.sessionDropY();
    return { x, y, w, h };
  }

  placeSession(id: string, driverId: string): Slot {
    const existing = this.sessSlot.get(id);
    if (existing) return existing;
    const driverSlot = this.slotFor(driverId) ?? this.placeTask(driverId);
    const list = this.sessOrder.get(driverId) ?? [];
    const idx = list.length;
    list.push(id);
    this.sessOrder.set(driverId, list);
    const slot = this.sessionSlotAt(driverSlot, idx);
    this.sessSlot.set(id, slot);
    return slot;
  }

  /** Round 5 item 0 fix (part B): follows reslotAsSubtask. A session card
   * is placed relative to its driver's slot AT THE MOMENT the session is
   * first seen (placeSession, above) -- if that happens before the
   * driver's own true kind/parent is known (routine during a staggered
   * fan-out: the sim starts driving a subtask the SAME tick it creates
   * it, well inside the ~2s self-heal debounce window), the session gets
   * anchored to the driver's WRONG root-column slot. reslotAsSubtask then
   * deletes that slot and re-places the driver, but had no way to know
   * which session ids were anchored to it -- so a session born early
   * stayed frozen at coordinates describing nothing, and its wire never
   * lined up with its (now-moved) driver. Called right after
   * reslotAsSubtask from GraphState.reconcile(); recomputes every
   * session already anchored to `driverId` against its NEW slot and
   * returns the updated {id, slot} pairs so the caller can also start a
   * fresh spawn-in slide for each LiveNode (this class only owns layout
   * math, never animation state). */
  reslotSessionsOf(driverId: string): { id: string; slot: Slot }[] {
    const ids = this.sessOrder.get(driverId);
    if (!ids || !ids.length) return [];
    const driverSlot = this.taskSlot.get(driverId) ?? this.subSlot.get(driverId);
    if (!driverSlot) return [];
    return ids.map((id, idx) => {
      const slot = this.sessionSlotAt(driverSlot, idx);
      this.sessSlot.set(id, slot);
      return { id, slot };
    });
  }

  /** Round 3 item-1 residual fix (found while verifying auto-fit against
   * a real scenario run): every SSE event handler's ensureTaskNode call
   * omits the kind/parentTaskId args (graphState.ts's applyEvent has no
   * way to know them -- WorkEvent carries only a bare task_id), so a
   * subtask's FIRST-seen live event -- routinely its very first
   * heartbeat/tokens.turn/agent.run, well before the ~2s debounced
   * self-heal reconcile could ever adopt it correctly -- places it via
   * placeTask() as an extra top-level ROW in the root task's own column
   * instead of fanning beside its real parent. Verified live: a 3-
   * sibling subtask fan rendered as ONE tall narrow column of unrelated-
   * looking "root tasks", which is exactly what defeated auto-fit's
   * ability to actually fill a landscape viewport (content stayed
   * portrait-shaped no matter how well the camera fit it). Called from
   * GraphState.reconcile() the FIRST time a self-heal snapshot reveals
   * the node's true kind/parent -- discards the wrong taskSlot entry
   * (and its taskOrder row) so bounds()/future placeTask() rows don't
   * carry the ghost of the wrong slot, then places it fresh as a real
   * subtask. */
  reslotAsSubtask(id: string, parentTaskId: string): Slot {
    if (this.taskSlot.has(id)) {
      this.taskSlot.delete(id);
      this.taskOrder = this.taskOrder.filter((t) => t !== id);
    }
    return this.placeSubtask(id, parentTaskId);
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
