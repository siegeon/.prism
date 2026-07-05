/**
 * domainTone — the SINGLE source of truth for domain color-coding.
 *
 * Every entity in the SPA (task status, memory type/status/classification,
 * priority band, importance band, audit action, conductor step, SDLC gate)
 * maps to one of the seven Hermes tones declared as --accent-{tone}-{bg,
 * ring,fg} triples in src/index.css. Before this file those maps were
 * copy-pasted across TasksPage / TaskDetailPage / ConductorPage /
 * MemoryPage / MemoryDetailPage / OkfPage / UnderstandPage / workflowChips.
 * They now live here once. Do NOT re-introduce a per-page tone map.
 *
 * Canvas/SVG surfaces (lib/palette.ts, components/Chart.tsx) keep their own
 * hex mirrors on purpose — they can't read CSS vars — and are excepted from
 * the palette ESLint guard. Everything else routes through here.
 */

/** The seven palette tones — matches --accent-{tone}-{bg,ring,fg}. */
export type Tone =
  | "teal" | "sage" | "amber" | "rose" | "violet" | "emerald" | "slate";

/** Task lifecycle status → tone (Tasks / TaskDetail / Conductor boards). */
const TASK_STATUS: Record<string, Tone> = {
  pending: "amber",
  in_progress: "teal",
  blocked: "rose",
  done: "emerald",
};

/** Memory lifecycle status → tone (Memory / MemoryDetail). */
const MEMORY_STATUS: Record<string, Tone> = {
  active: "emerald",
  stale: "amber",
  retired: "slate",
  archived: "slate",
  needs_review: "amber",
};

/** Memory concept type → tone. Superset union of the (identical where they
 * overlapped) maps that lived in MemoryPage/MemoryDetailPage (no `note`) and
 * OkfPage (no `user`) / UnderstandPage. No key ever conflicted, so folding
 * them changes no existing assignment. */
const MEMORY_TYPE: Record<string, Tone> = {
  expertise: "teal",
  convention: "sage",
  decision: "amber",
  "anti-pattern": "rose",
  feedback: "violet",
  project: "amber",
  reference: "teal",
  user: "violet",
  pattern: "teal",
  failure: "rose",
  note: "slate",
};

/** Memory classification band → tone (MemoryPage). */
const CLASSIFICATION: Record<string, Tone> = {
  foundational: "violet",
  tactical: "sage",
  strategic: "amber",
};

/** Task audit-log action → tone (TaskDetailPage history). */
const ACTION: Record<string, Tone> = {
  created: "violet",
  updated: "slate",
  advance_task: "teal",
  gate_decide: "amber",
};

/** Conductor workflow step id → tone (workflowChips step chips). */
const STEP: Record<string, Tone> = {
  intake: "violet",
  review_previous_notes: "teal",
  draft_story: "teal",
  story_gate: "slate",
  verify_plan: "teal",
  plan_gate: "slate",
  write_failing_tests: "violet",
  red_gate: "slate",
  implement_tasks: "amber",
  verify_green_state: "violet",
  green_gate: "slate",
};

/** SDLC gate state → tone (workflowChips gate chips). */
const GATE: Record<string, Tone> = {
  none: "slate",
  pending: "amber",
  passed: "emerald",
  failed: "rose",
};

const MAPS = {
  taskStatus: TASK_STATUS,
  memoryStatus: MEMORY_STATUS,
  memoryType: MEMORY_TYPE,
  classification: CLASSIFICATION,
  action: ACTION,
  step: STEP,
  gate: GATE,
} as const;

/** Priority band → tone. Lower number = higher priority; non-numeric
 * strings fall through to the deterministic label hash (toneFromLabel). */
export function priorityTone(p: number | string | undefined | null): Tone {
  if (p === undefined || p === null) return "slate";
  const n = typeof p === "number" ? p : Number(p);
  if (!Number.isFinite(n)) return toneFromLabel(String(p));
  if (n <= 1) return "rose";
  if (n === 2) return "amber";
  if (n === 3) return "sage";
  if (n === 4) return "violet";
  return "slate";
}

/** Importance 1-10 → tone dot: muted slate → sage → amber → rose. */
export function importanceTone(n: number): Tone {
  if (n >= 9) return "rose";
  if (n >= 7) return "amber";
  if (n >= 4) return "sage";
  return "slate";
}

/** Deterministic label → tone hash. Used for user-defined axes (domains)
 * and as the type-tone fallback on graph surfaces. "all" → slate so the
 * universal-filter pill never borrows a meaningful hue. */
const HASH_TONES: Tone[] = ["teal", "sage", "amber", "rose", "violet", "emerald"];
export function toneFromLabel(label: string): Tone {
  if (label === "all") return "slate";
  let h = 0;
  for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) >>> 0;
  return HASH_TONES[h % HASH_TONES.length];
}

/** Memory type → tone with a hash fallback for unknown types. This is the
 * OkfPage/UnderstandPage graph-node behavior (map hit, else hash) — kept
 * distinct from the list pages, which slate-fall-back unknown types. */
export function typeToneHashed(type: string): Tone {
  const t = (type || "").toLowerCase();
  return MEMORY_TYPE[t] ?? toneFromLabel(t);
}

export type DomainKind =
  | "taskStatus" | "memoryStatus" | "memoryType"
  | "classification" | "action" | "step" | "gate"
  | "priority" | "importance";

/**
 * Resolve an entity value to its canonical tone. Map-backed kinds return
 * `undefined` on a miss so callers can preserve their existing fallback
 * (some coalesce to "slate", filter pills intentionally leave it neutral).
 */
export function domainTone(
  kind: DomainKind,
  value: string | number | undefined | null,
): Tone | undefined {
  if (kind === "priority") return priorityTone(value as number | string | undefined);
  if (kind === "importance") return importanceTone(Number(value));
  if (value === undefined || value === null) return undefined;
  return MAPS[kind][String(value)];
}

/** `var(--accent-<tone>-<slot>)` — the value form for inline styles. */
export function toneVar(tone: Tone, slot: "bg" | "fg" | "ring"): string {
  return `var(--accent-${tone}-${slot})`;
}

/** Tailwind class triple (bg fill + fg text + ring) for a tone. The shared
 * badge/chip styling that stepChipClass/gateChipClass build on. */
export function toneClass(tone: Tone): string {
  return [
    `bg-[color:var(--accent-${tone}-bg)]`,
    `text-[color:var(--accent-${tone}-fg)]`,
    `ring-1 ring-[color:var(--accent-${tone}-ring)]`,
  ].join(" ");
}
