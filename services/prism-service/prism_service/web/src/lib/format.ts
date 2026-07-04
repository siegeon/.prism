// Shared compact number formatter for token counts. v6.7.23: token totals
// now sum ALL four usage fields (output + input + cache_read +
// cache_creation), so real values run ~200x larger than the old output-only
// numbers — billions, not thousands. One k/M/B formatter so every surface
// (conductor tile, task timeline, workflow lanes, dashboards) renders them
// identically and compactly.
export function fmtTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n >= 1e9) return `${(n / 1e9).toFixed(n >= 1e11 ? 0 : 1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1)}k`;
  return String(n);
}
