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
export function fmtTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n >= 999.5e6) return `${(n / 1e9).toFixed(n >= 99.95e9 ? 0 : 1)}B`;
  if (n >= 999.5e3) return `${(n / 1e6).toFixed(n >= 99.95e6 ? 0 : 1)}M`;
  if (n >= 999.5) return `${(n / 1e3).toFixed(n >= 99.95e3 ? 0 : 1)}k`;
  return String(Math.round(n));
}

// Line-anchored: matches a WHOLE line "Mirrored to <provider> <issue>."
// or "Mirrored from <provider> <source>." (both push and import shapes),
// never a line that merely CONTAINS the words mid-sentence (task
// 675dd11a, AC-5 — "We mirrored to github in Q3..." must survive).
const MIRROR_PROVENANCE_RE = /^Mirrored (to|from) \S+ .*\.$/;
const BARE_URL_RE = /^https?:\/\/\S+$/;

// Render-side only: strips mirror-provenance prose (and its following
// bare-URL line, if any) out of a task's description before it reaches
// <Markdown>. The STORED description is never rewritten — live mirror
// badges are store-derived (task 6fbbec35) and legacy rows still need
// the raw text for api/tasks.py's _mirror_url derivation (task 675dd11a
// FR-6). Collapses any resulting 3+ run of newlines down to 2 so a
// removed block never leaves a stack of blank lines behind.
export function stripMirrorProvenance(
  text: string | null | undefined,
): string {
  if (!text) return "";
  const lines = text.split("\n");
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    if (MIRROR_PROVENANCE_RE.test(lines[i])) {
      if (i + 1 < lines.length && BARE_URL_RE.test(lines[i + 1])) i++;
      continue;
    }
    out.push(lines[i]);
  }
  return out.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}
