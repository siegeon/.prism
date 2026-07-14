/**
 * ViolationsView — the violations-analyzer card on /understand (d2,
 * task 8579d49e). Renders the intended-vs-observed architecture
 * conformance verdict (violations.json, computed per SHA by
 * arc_governance.compute_violations from Brain-stored principles vs the
 * architecture_analyzer's layers.json edges).
 *
 * Progressive disclosure: leads with a count summary; the per-violation
 * edge list opens on click (local open state), never a wall of text.
 */
import { useState } from "react";
import { ChevronDown, ChevronRight, ShieldAlert, ShieldCheck } from "lucide-react";
import { Empty, SectionLabel } from "@/components/ui";

type Violation = { from?: string; to?: string; principle?: string };

type Violations = {
  count?: number;
  violations?: Violation[];
};

export default function ViolationsView({ data }: { data: Violations }) {
  const [open, setOpen] = useState(false);
  const count = data.count ?? data.violations?.length ?? 0;
  const items = data.violations ?? [];

  if (count === 0) {
    return (
      <div className="flex items-center gap-3 text-sm text-[color:var(--text-secondary)]">
        <ShieldCheck className="w-4 h-4 text-[color:var(--accent-sage-fg)]" />
        <span>
          <span className="font-medium">0 violations</span> — observed layer
          edges conform to every seeded architecture principle.
        </span>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-3 text-left w-full"
      >
        {open
          ? <ChevronDown className="w-3.5 h-3.5 opacity-60 shrink-0" />
          : <ChevronRight className="w-3.5 h-3.5 opacity-60 shrink-0" />}
        <ShieldAlert className="w-4 h-4 text-[color:var(--accent-amber-fg)] shrink-0" />
        <span className="text-sm">
          <span className="font-medium">{count} principle violation{count === 1 ? "" : "s"}</span>
          <span className="opacity-60"> — intended vs observed layer edges.
            Click to {open ? "collapse" : "expand"}.</span>
        </span>
      </button>

      {open && (
        <div className="pl-7">
          <SectionLabel>Forbidden edges observed</SectionLabel>
          <ul className="mt-2 space-y-1.5">
            {items.map((v, i) => (
              <li
                key={`${v.from}-${v.to}-${i}`}
                className="flex items-center gap-2 text-[12px] rounded-md border border-[color:var(--midground-base)]/15 bg-[color:var(--background-base)]/40 px-3 py-2"
              >
                <span className="font-mono">{v.from ?? "—"}</span>
                <span className="opacity-50">→</span>
                <span className="font-mono">{v.to ?? "—"}</span>
                <span className="ml-auto text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)] ring-1 ring-inset ring-[color:var(--accent-amber-ring)]">
                  {v.principle ?? "principle"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function ViolationsEmpty() {
  return <Empty>No violations artifact yet — run a refresh to compute it.</Empty>;
}
