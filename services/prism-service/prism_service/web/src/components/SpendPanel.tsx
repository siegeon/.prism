// Honest per-field, per-turn-model dollar SPEND panel (task ff160371).
// Cumulative $ over the task's linked-session transcripts, split MAIN
// (this task's own turns) vs BACKGROUND (nested workflow-subagent turns —
// previously folded silently into the parent total everywhere else in the
// app). Distinct surface from TokenTurns (the conductor tile's tok/s
// BURN-RATE graph): this is a cumulative dollar TOTAL, not a rate, so the
// two never duplicate each other.
import { Card, Empty, SectionLabel, Kpi } from "@/components/ui";

export interface SpendSection {
  tokens: number;
  components: Record<string, number>;
  usd: number;
  unpriced_tokens: number;
  priced: boolean;
}

export interface SpendData {
  main: SpendSection;
  background: SpendSection;
  total: SpendSection;
  priced: boolean;
}

const FIELD_LABELS: Array<[string, string]> = [
  ["input_tokens", "input"],
  ["output_tokens", "output"],
  ["cache_read_input_tokens", "cache read"],
  ["cache_creation_input_tokens", "cache write"],
];

function fmtUsd(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function fmtTok(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(Math.round(n));
}

export default function SpendPanel({ spend }: { spend?: SpendData | null }) {
  if (!spend || spend.total.tokens <= 0) {
    return (
      <Card>
        <SectionLabel>Spend</SectionLabel>
        <Empty>No measured spend yet — this task has no linked-session transcript usage on disk.</Empty>
      </Card>
    );
  }
  const { main, background, total } = spend;
  const mainPct = total.usd > 0
    ? Math.min(100, Math.max(main.usd > 0 ? 1.5 : 0, (100 * main.usd) / total.usd))
    : 0;
  const bgPct = total.usd > 0 ? 100 - mainPct : 0;

  return (
    <Card>
      <div className="flex items-baseline justify-between gap-2 mb-3">
        <SectionLabel>Spend</SectionLabel>
        <span className="text-2xs text-[color:var(--text-muted)]">measured from transcript</span>
      </div>

      <div className="text-2xl font-[650] tabular-nums text-[color:var(--text-primary)] mb-3">
        {fmtUsd(total.usd)}
      </div>

      {/* main vs background split — stacked bar + two labeled figures. The
          split is the whole point of this panel: background spend must be
          SEEN, never folded silently back into one number. */}
      <div className="space-y-1 mb-3">
        <div className="h-[7px] flex rounded-[4px] overflow-hidden bg-[color:var(--surface-3)]">
          {mainPct > 0 && <span style={{ width: `${mainPct}%`, background: "var(--accent-teal-fg)" }} />}
          {bgPct > 0 && <span style={{ width: `${bgPct}%`, background: "var(--accent-amber-fg)" }} />}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-2xs font-mono tabular-nums">
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full shrink-0" style={{ background: "var(--accent-teal-fg)" }} />
            <span className="text-[color:var(--text-secondary)]">main</span>
            <span className="text-[color:var(--text-muted)]">{fmtUsd(main.usd)} · {fmtTok(main.tokens)} tok</span>
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full shrink-0" style={{ background: "var(--accent-amber-fg)" }} />
            <span className="text-[color:var(--text-secondary)]">background</span>
            <span className="text-[color:var(--text-muted)]">{fmtUsd(background.usd)} · {fmtTok(background.tokens)} tok</span>
          </span>
        </div>
      </div>

      {/* 4-field token breakdown — priced PER FIELD, never a flat rate. */}
      <div className="flex flex-wrap gap-2">
        {FIELD_LABELS.map(([field, label]) => (
          <Kpi key={field} label={label} value={fmtTok(total.components[field] ?? 0)} />
        ))}
      </div>

      {!spend.priced && (
        <div className="mt-3 text-2xs text-[color:var(--accent-amber-fg)]">
          {fmtTok(total.unpriced_tokens)} tokens priced at an unrecognized-model default rate — not a sourced price.
        </div>
      )}
    </Card>
  );
}
