/** Round 4 item 2 (throughput bar honesty, second offense): ONE shared
 * fixed-anchor log scale, used by BOTH the HUD hero meter (hud.ts) and
 * every card's per-node throughput bar (cards.ts, via graphState.ts's
 * bufferFrac). Pips at 10/100/1k/10k tok/s never move — length is
 * monotonic with the printed number, always, everywhere a tok/s value is
 * gauged on this canvas.
 *
 * Round 3 already fixed this for the HUD hero meter alone (logMeterFrac
 * used to live in hud.ts). Round 4's critic caught the SAME bug still
 * live on every task/subtask card's own buffer bar: cards.ts's bar
 * scaled against a per-card ROLLING PEAK (bufferPeak/bufferLevelFrac in
 * graphState.ts), so a card's own rising peak shrank its bar even as its
 * printed rate grew ("t=24s: 3.5K [446/s] bar ~68%; t=30s: 5.3K [470/s] —
 * a bigger number — bar collapsed to ~27%"). Extracting the scale into
 * this one module and pointing BOTH consumers at it is the actual fix —
 * two call sites, one law, instead of two scales that can silently
 * diverge again. */

export const LOG_METER_MIN = 1; // floor -- anything below this still reads as "just above empty"
export const LOG_METER_MAX = 10_000;
export const LOG_METER_PIPS = [10, 100, 1_000, 10_000];

export function logMeterFrac(value: number): number {
  if (value <= 0) return 0;
  const clamped = Math.max(LOG_METER_MIN, Math.min(LOG_METER_MAX, value));
  return Math.log10(clamped / LOG_METER_MIN) / Math.log10(LOG_METER_MAX / LOG_METER_MIN);
}

export function logMeterPipFrac(pipValue: number): number {
  return Math.log10(pipValue / LOG_METER_MIN) / Math.log10(LOG_METER_MAX / LOG_METER_MIN);
}
