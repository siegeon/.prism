/**
 * Shared PRISM v5 primitives — Slate-Blue Hermes cards, KPIs, labels,
 * pills, tables.
 *
 * Author against the semantic tokens defined in index.css (--surface-*,
 * --text-*, --border-*) rather than --midground-base/N opacity sleeves.
 * The token tiers are what give the design its hierarchy: cards stand
 * out from page (surface-0 → surface-1), nested cards stand out from
 * their parent (surface-1 → surface-2), and every text tier is WCAG
 * AA on both surfaces.
 */
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/utils";
import type { Tone } from "@/lib/domainTone";
import { toneFromLabel } from "@/lib/domainTone";

// Re-exported so existing `import { toneFromLabel } from "@/components/ui"`
// call sites keep working; the implementation lives in lib/domainTone.
export { toneFromLabel };

export const Card = ({
  children, className, nested, raised,
}: {
  children: ReactNode;
  className?: string;
  /** Use when the card sits INSIDE another Card — lifts to surface-2. */
  nested?: boolean;
  /** Use for the dominant card on a page — lifts to surface-2 + strong border. */
  raised?: boolean;
}) => {
  const surfaceClass = nested || raised
    ? "bg-[color:var(--surface-2)]"
    : "bg-[color:var(--surface-1)]";
  const borderClass = raised
    ? "border-[color:var(--border-strong)]"
    : "border-[color:var(--border-default)]";
  return (
    <div className={cn("rounded-md border p-5", surfaceClass, borderClass, className)}>
      {children}
    </div>
  );
};

export const SectionLabel = ({ children }: { children: ReactNode }) => (
  <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)] mb-3">
    {children}
  </div>
);

export const Kpi = ({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) => (
  <div className="flex-1 min-w-[150px] rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] p-4">
    <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)] mb-2">{label}</div>
    <div className="text-2xl font-semibold leading-none text-[color:var(--text-primary)]">{value}</div>
    {hint && <div className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] mt-2">{hint}</div>}
  </div>
);

/** Tone names matching the --accent-{name}-{bg,ring,fg} token triples
 * in index.css. When a Pill is given a tone AND is active, the pill
 * inherits that accent — keeping filter UI and the entries it filters
 * speaking the same color language. Inactive pills stay neutral
 * regardless of tone so the filter strip doesn't shout when nothing
 * is selected.
 */
// Alias of the single Tone source in lib/domainTone — kept as a named
// export so the many `type PillTone` importers stay unchanged.
export type PillTone = Tone;

const PILL_TONE_ACTIVE: Record<PillTone, string> = {
  teal:    "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)] ring-1 ring-inset ring-[color:var(--accent-teal-ring)]",
  sage:    "bg-[color:var(--accent-sage-bg)] text-[color:var(--accent-sage-fg)] ring-1 ring-inset ring-[color:var(--accent-sage-ring)]",
  amber:   "bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)] ring-1 ring-inset ring-[color:var(--accent-amber-ring)]",
  rose:    "bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] ring-1 ring-inset ring-[color:var(--accent-rose-ring)]",
  violet:  "bg-[color:var(--accent-violet-bg)] text-[color:var(--accent-violet-fg)] ring-1 ring-inset ring-[color:var(--accent-violet-ring)]",
  emerald: "bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)] ring-1 ring-inset ring-[color:var(--accent-emerald-ring)]",
  slate:   "bg-[color:var(--accent-slate-bg)] text-[color:var(--accent-slate-fg)] ring-1 ring-inset ring-[color:var(--accent-slate-ring)]",
};

// Inactive-with-tone: keep the neutral surface-2 background (so the
// strip doesn't shout) but tint the TEXT with the accent's fg color
// so the pill reads as a legend at-a-glance — you can see "expertise
// = teal, decision = amber" without clicking. On hover we lift the
// bg slightly toward the accent fill so it still feels actionable.
const PILL_TONE_INACTIVE: Record<PillTone, string> = {
  teal:    "bg-[color:var(--surface-2)] text-[color:var(--accent-teal-fg)] hover:bg-[color:var(--accent-teal-bg)]",
  sage:    "bg-[color:var(--surface-2)] text-[color:var(--accent-sage-fg)] hover:bg-[color:var(--accent-sage-bg)]",
  amber:   "bg-[color:var(--surface-2)] text-[color:var(--accent-amber-fg)] hover:bg-[color:var(--accent-amber-bg)]",
  rose:    "bg-[color:var(--surface-2)] text-[color:var(--accent-rose-fg)] hover:bg-[color:var(--accent-rose-bg)]",
  violet:  "bg-[color:var(--surface-2)] text-[color:var(--accent-violet-fg)] hover:bg-[color:var(--accent-violet-bg)]",
  emerald: "bg-[color:var(--surface-2)] text-[color:var(--accent-emerald-fg)] hover:bg-[color:var(--accent-emerald-bg)]",
  slate:   "bg-[color:var(--surface-2)] text-[color:var(--accent-slate-fg)] hover:bg-[color:var(--accent-slate-bg)]",
};

export const Pill = ({
  children, active, onClick, tone,
}: {
  children: ReactNode;
  active?: boolean;
  onClick?: () => void;
  /** When set, the pill adopts the tone's accent — solid fill when
   * active, tinted text + neutral bg when inactive (legend mode). */
  tone?: PillTone;
}) => (
  <button
    onClick={onClick}
    className={cn(
      "px-3 py-1 rounded-full text-2xs uppercase tracking-wider transition-colors",
      active
        ? tone
          ? PILL_TONE_ACTIVE[tone]
          : "bg-[color:var(--text-primary)] text-[color:var(--surface-0)]"
        : tone
          ? PILL_TONE_INACTIVE[tone]
          : "bg-[color:var(--surface-2)] text-[color:var(--text-secondary)] hover:bg-[color:var(--surface-3)] hover:text-[color:var(--text-primary)]",
    )}
  >{children}</button>
);

/** Button — the artifact's `.btn` / `.btn.pri`. Two variants:
 *  - "primary": solid --accent-solid fill, white text (the page's one CTA).
 *  - "default": bordered --border-default on the subtle surface (secondary).
 *  13px / 550 weight, 6px radius. Standard <button> props pass through. */
export const Button = ({
  variant = "default", className, children, ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "default";
}) => (
  <button
    className={cn(
      "inline-flex items-center gap-1.5 rounded-md px-[13px] py-[6px] text-[13px] font-[550]",
      "transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
      variant === "primary"
        ? "bg-[color:var(--accent-solid)] text-white border border-[color:var(--accent-solid)] hover:opacity-90"
        : "bg-[color:var(--surface-1)] text-[color:var(--text-secondary)] border border-[color:var(--border-default)] hover:border-[color:var(--border-strong)] hover:text-[color:var(--text-primary)]",
      className,
    )}
    {...rest}
  >{children}</button>
);

export const Empty = ({ children }: { children: ReactNode }) => (
  <div className="rounded-md border border-dashed border-[color:var(--border-default)] px-5 py-8 text-center text-sm text-[color:var(--text-muted)]">
    {children}
  </div>
);

export const Page = ({ children }: { children: ReactNode }) => (
  // Full-width: page fills the available content column; minimum is
  // whatever the children require. (Was capped at 1400px previously,
  // which left big empty gutters on wider monitors.)
  <div className="p-8 space-y-6 w-full min-w-[720px]">{children}</div>
);

export const ErrorBanner = ({ children }: { children: ReactNode }) => (
  <div className="rounded-md border border-[color:var(--accent-rose-ring)] bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] px-4 py-3 text-sm">
    {children}
  </div>
);
