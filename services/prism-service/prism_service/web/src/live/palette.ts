/** Locked color language for the /live canvas (DESIGN_DIRECTIVE.md +
 * reference/VISUAL_GRAMMAR.md). This canvas is a distinct "game-like"
 * surface, not app chrome, so it uses literal hex per the directive
 * instead of the app's Radix theme tokens — the whole point is a fixed,
 * never-reused-for-two-meanings palette regardless of light/dark theme.
 *
 * teal   = token flow (data)
 * orange = compute / step progress / selection outline
 * green  = money/spend + passed gates
 * red    = RESERVED for dead (disconnected/no driver/failed), never "slow"
 * magenta = needs a decision (gate awaiting a distinct actor)
 */
export const PALETTE = {
  ground: "#1c2230",
  grid: "rgba(255,255,255,0.05)",
  card: "#262c3c",
  cardTitle: "#323a4f",
  cardTitleDone: "#2a3a33",
  border: "rgba(255,255,255,0.06)",

  teal: "#2dd4bf",
  orange: "#f59e0b",
  green: "#34d399",
  red: "#ef4444",
  magenta: "#e879f9",

  selection: "#ff8a3d",

  textPrimary: "#edf1f7",
  textLabel: "#9aa6bd",
  textDim: "#6b7280",

  // Round 3 item-0/3 fix: the old packet color (#5eead4) sat in the SAME
  // teal hue family as a live wire's #2dd4bf, so a 6x6 marker riding a
  // bright 0.9-alpha teal wire had almost no hue contrast -- verified
  // live: instrumentation confirmed markers WERE spawning (30 in a 24s
  // window against real flow) yet round2's critics still called them
  // "rare/subtle" or missed them outright. A marker is a MOTION cue, not
  // a resource-type color, so it gets its own near-white highlight with a
  // dark outline (packetOutline) instead of reusing any of the 5 locked
  // semantic hues -- pops against a teal wire, a dim neutral wire, and an
  // orange structural wire alike.
  packet: "#fef9e7",
  packetOutline: "#0f1420",
} as const;

/** Round 3 item 2 (node type vocabulary): a session/agent card's glyph
 * now varies by ROLE (dev/qa/sm), not just kind -- distinct silhouettes
 * so the legend chip (hud.ts's drawLegend) can name all of them at a
 * glance. `role` is optional/best-effort (api/work.py's session node
 * carries it off the latest agent_runs row; a session with no telemetry
 * yet falls back to the generic dev/agent dot). */
export function glyphFor(kind: "task" | "subtask" | "session", role?: string | null): string {
  if (kind === "task") return "▣"; // ▣ root task
  if (kind === "subtask") return "◇"; // ◇ subtask
  if (role === "qa") return "▲"; // ▲ verifier
  if (role === "sm") return "■"; // ■ steward
  return "●"; // ● dev / unknown agent
}

/** Round 2, piece 3 (node state while working): the five states a card
 * can be in, and the ONE place their chrome maps to color. Red stays
 * reserved for "stalled" here and here only — the round1 critic's exact
 * complaint was "a red dot sits next to Step/Spend on every card whether
 * it's active or not, so red is never reserved for 'dead', it's just
 * noise". A connector dot with no live signal falls back to THIS
 * function's color, never a hardcoded PALETTE.red, so a young/pending
 * card (never signaled yet) or a healthy card with a not-yet-ticked
 * stat (e.g. Spend at $0 on a fresh task) reads neutral-dim instead of
 * alarm-red. */
export type CardState = "working" | "waiting_gate" | "stalled" | "young" | "done";

/** Round 4 item 1c SUPERSEDES round1's unconditional "stalled -> red"
 * rule: critic 1's residual was "red 'Xs since signal' stacking on every
 * node with no recovery -- reads as a system crash, not a live graph
 * with one blocked path". Root cause: when the WHOLE queue goes quiet,
 * every card's lastSignalAt stops advancing at roughly the same moment,
 * so every card crosses STALL_MS together and turns red in lockstep --
 * indistinguishable from an actual mass failure. Red stays reserved for
 * a card that's dead WHILE OTHERS ARE STILL WORKING (a genuinely
 * anomalous, locally-blocked path); once the graph as a whole reads
 * quiet (`graphQuiet`, draw.ts's isGraphQuiet), a stalled card downgrades
 * to the same neutral treatment as YOUNG -- still visibly "not fresh"
 * (the ring/border still draws, still fades with signalFreshness), just
 * not an alarm, because silence-while-everything-is-silent is expected,
 * not a crash. `deriveCardState` itself is untouched -- a card is still
 * STRUCTURALLY "stalled" (its own signal really did go stale); only the
 * COLOR this function hands back is context-dependent, per the build
 * directive: "keep them ticking... but their COLOR depends on context." */
export function deadRingColorFor(state: CardState, graphQuiet = false): string {
  if (state === "stalled") return graphQuiet ? PALETTE.textDim : PALETTE.red;
  if (state === "waiting_gate") return PALETTE.magenta;
  return PALETTE.textDim; // young / working / done — never red
}

export function titleTintFor(state: CardState, graphQuiet = false): string {
  if (state === "waiting_gate") return "#3a2f45"; // magenta-tinted slate
  if (state === "stalled") return graphQuiet ? PALETTE.cardTitle : "#2e2a2a"; // neutral while the whole queue is quiet
  if (state === "done") return PALETTE.cardTitleDone;
  if (state === "working") return "#2c3550"; // teal-tinted slate
  return PALETTE.cardTitle; // young — the plain neutral default
}
