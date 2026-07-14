/**
 * Lozenge — the artifact's `.lz`: a tiny, bordered, uppercase status chip.
 *
 * Usage:
 *   <Lozenge tone="info">In progress</Lozenge>
 *   <Lozenge tone="ok">Passed</Lozenge>
 *   <Lozenge tone="danger">Failed</Lozenge>
 *   <Lozenge tone="neutral" className="ml-2">implement</Lozenge>
 *
 * Every color comes from the --accent-{tone}-{bg,ring,fg} token triples in
 * index.css (workstream 1), so each pair holds WCAG AA in both themes.
 * tone → accent family: neutral→slate, info→teal, ok→sage, warn→amber,
 * danger→rose, new→violet.
 */
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export type LozengeTone =
  | "neutral" | "info" | "ok" | "warn" | "danger" | "new";

// tone → the accent triple it borrows (bg fill · ring border · fg text).
const TONE_CLASS: Record<LozengeTone, string> = {
  neutral: "bg-[color:var(--accent-slate-bg)] text-[color:var(--accent-slate-fg)] border-[color:var(--accent-slate-ring)]",
  info:    "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)] border-[color:var(--accent-teal-ring)]",
  ok:      "bg-[color:var(--accent-sage-bg)] text-[color:var(--accent-sage-fg)] border-[color:var(--accent-sage-ring)]",
  warn:    "bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)] border-[color:var(--accent-amber-ring)]",
  danger:  "bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] border-[color:var(--accent-rose-ring)]",
  new:     "bg-[color:var(--accent-violet-bg)] text-[color:var(--accent-violet-fg)] border-[color:var(--accent-violet-ring)]",
};

export const Lozenge = ({
  tone, children, className,
}: {
  tone: LozengeTone;
  children: ReactNode;
  className?: string;
}) => (
  // text-2xs = 11px, the app-wide floor (the artifact's .lz is 10.5px, but
  // this repo forbids sub-11px text). 2px/7px padding, 3px radius, 1px border.
  <span
    className={cn(
      "inline-flex items-center gap-1 whitespace-nowrap rounded-[3px] border px-[7px] py-[2px]",
      "text-2xs font-bold uppercase tracking-wide",
      TONE_CLASS[tone],
      className,
    )}
  >
    {children}
  </span>
);
