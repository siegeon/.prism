/**
 * Shared accent palette — JS mirror of the --accent-{tone}-{bg,ring,fg}
 * tokens declared in src/index.css. CSS variables can't be read from
 * SVG/canvas, and graphify's Sigma viewer is a server-rendered HTML
 * blob that doesn't see them at all, so the same hexes have to be
 * available as JS constants. Keep this list aligned with index.css —
 * if you add or rename a tone, update both files in the same commit.
 *
 * Used by:
 *   - PrismLayerNode (architecture graph node fill)
 *   - PrismDomainNode (architecture graph parent node fill)
 *   - graph_static.py community palette (Python-side hex list mirrors
 *     ACCENT_HEX + ACCENT_HEX_LIFTED in the same order, see
 *     "// COMMUNITY_COLORS — sourced from web/src/lib/palette.ts").
 */

/** Saturated foreground hexes — match --accent-{tone}-fg. */
export const ACCENT_HEX = [
  "#5eead4", // teal
  "#a3d9a5", // sage
  "#fcd34d", // amber
  "#f9a8d4", // rose
  "#c4b5fd", // violet
  "#6ee7b7", // emerald
  "#cbd5e1", // slate
] as const;

/** Same hues, lightness lifted ~10 — used as a second tier so palettes
 * that need more than seven distinct swatches (community graph, lifted
 * domain cards) don't immediately repeat the base hue. */
export const ACCENT_HEX_LIFTED = [
  "#8ff5e6", // teal lifted
  "#bee5c0", // sage lifted
  "#fde58a", // amber lifted
  "#fbc7e5", // rose lifted
  "#d6cdfd", // violet lifted
  "#9ef0c9", // emerald lifted
  "#dde4eb", // slate lifted
] as const;

/** Hex (#rrggbb) → rgba(r,g,b,a). Used to derive border + fill alphas
 * from a base hex without authoring rgba strings by hand. */
export function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.substring(0, 2), 16);
  const g = parseInt(h.substring(2, 4), 16);
  const b = parseInt(h.substring(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}
