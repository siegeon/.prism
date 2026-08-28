// Shared compact number formatter for token counts. v6.7.23: token totals
// now sum ALL four usage fields (output + input + cache_read +
// cache_creation), so real values run ~200x larger than the old output-only
// numbers — billions, not thousands. One k/M/B formatter so every surface
// (conductor tile, task timeline, workflow lanes, dashboards) renders them
// identically and compactly.
//
// Tier boundaries are chosen so ROUNDING PROMOTES across them: 999,500
// renders "1.0M" (never "1000k") and 999,950,000 renders "1.0B" (never
// "1000M") — each tier starts at the smallest value whose 1-decimal
// rendering would round up to 1.0 of the next unit.
// No JS test rig exists in web/ (no vitest/jest); boundary cases are
// documented + manually exercised: 999_499 -> "999k", 999_500 -> "1.0M",
// 99_950_000 -> "100M", 999_500_000 -> "1.0B".
/** Dollars for the whole SPA. Sub-cent runs are real (a single cheap step
 * lands around $0.003), so they get 4 decimals rather than rounding to
 * "$0.00" and reading as free. Callers decide what ABSENCE looks like:
 * this formats a number it is given, and the Trace dashes instead of
 * calling it when no cost was attributed. */
export function fmtUsd(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

export function fmtTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n >= 999.5e6) return `${(n / 1e9).toFixed(n >= 99.95e9 ? 0 : 1)}B`;
  if (n >= 999.5e3) return `${(n / 1e6).toFixed(n >= 99.95e6 ? 0 : 1)}M`;
  if (n >= 999.5) return `${(n / 1e3).toFixed(n >= 99.95e3 ? 0 : 1)}k`;
  return String(Math.round(n));
}
