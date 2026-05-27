/**
 * relativeTime — render an ISO timestamp as a compact age string.
 *
 * "3s", "12m", "4h", "2d", "3w" — same bucketing the /conductor tiles
 * use to fit an age inside a single inline stat row. Empty / invalid
 * inputs return an em dash so the UI doesn't render NaN.
 */
export function relativeTime(iso: string): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 0) return "now";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d`;
  const w = Math.floor(d / 7);
  return `${w}w`;
}
