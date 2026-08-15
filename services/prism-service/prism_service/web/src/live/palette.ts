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

  packet: "#5eead4",
} as const;

export function glyphFor(kind: "task" | "subtask" | "session"): string {
  if (kind === "task") return "▣"; // ▣ root task
  if (kind === "subtask") return "◇"; // ◇ subtask
  return "●"; // ● session/agent
}
