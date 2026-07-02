/**
 * PI tool-renderer registry — per-tool expanded bodies for tool-call rows.
 *
 * Structure borrowed from @earendil-works/pi-web-ui (MIT, Mario Zechner):
 * its Map<toolName, renderer> registry replacing a one-size-fits-all tool
 * body. Reimplemented for React + Hermes tokens (CSS custom properties from
 * src/index.css — no hardcoded colors).
 *
 * Contract: a renderer returns null when the payload doesn't match the
 * shape it knows; the caller (ToolRow in PiAgentPanel) then falls back to
 * the default collapsed mono receipt. Every renderer keeps progressive
 * disclosure — one-line rows, click to expand the raw receipt.
 */
import { useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";

export type ToolRenderer = (result: unknown) => ReactNode | null;

// ------------------------------------------------------------- shared

export function prettyReceipt(value: unknown): string {
  if (value == null) return "(no result)";
  if (typeof value === "string") return value;
  try {
    const s = JSON.stringify(value, null, 2);
    return s.length > 4000 ? `${s.slice(0, 4000)}\n…(truncated)` : s;
  } catch {
    return String(value);
  }
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** Non-empty array of plain objects, else null (→ raw-receipt fallback). */
function asRows(result: unknown): Record<string, unknown>[] | null {
  if (!Array.isArray(result) || result.length === 0) return null;
  if (!result.every((x) => x !== null && typeof x === "object" && !Array.isArray(x))) return null;
  return result as Record<string, unknown>[];
}

function Receipt({ value }: { value: unknown }) {
  return (
    <div className="mx-1.5 my-1 px-2 py-1.5 rounded bg-[color:var(--surface-0)] border border-[color:var(--border-subtle)] max-h-40 overflow-y-auto">
      <div className="text-[10px] leading-relaxed font-mono text-[color:var(--text-secondary)] whitespace-pre-wrap break-words">
        {prettyReceipt(value)}
      </div>
    </div>
  );
}

// -------------------------------------------------------- brain_search

function BrainHitRow({ hit }: { hit: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const path = str(hit.source_file) || str(hit.doc_id) || "(unknown source)";
  const snippet = str(hit.content).split("\n").map((l) => l.trim()).find(Boolean) ?? "";
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full text-left px-1.5 py-1 rounded hover:bg-[color:var(--surface-2)] transition-colors"
      >
        <div className="text-[10px] font-mono text-[color:var(--accent-teal-fg)] truncate">{path}</div>
        {snippet && (
          <div className="text-[10px] font-mono text-[color:var(--text-muted)] truncate">{snippet}</div>
        )}
      </button>
      {open && <Receipt value={hit} />}
    </div>
  );
}

function renderBrainSearch(result: unknown): ReactNode | null {
  const hits = asRows(result);
  if (!hits) return null;
  return (
    <div className="space-y-0.5">
      {hits.map((h, i) => <BrainHitRow key={i} hit={h} />)}
    </div>
  );
}

// ------------------------------------------------------- memory_recall

function MemoryRow({ entry }: { entry: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const name = str(entry.name) || str(entry.id) || "memory";
  const domain = str(entry.domain);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-1.5 px-1.5 py-1 text-left rounded hover:bg-[color:var(--surface-2)] transition-colors min-w-0"
      >
        <span className="px-1.5 py-0.5 rounded-full text-[10px] font-mono truncate bg-[color:var(--accent-violet-bg)] text-[color:var(--accent-violet-fg)] ring-1 ring-inset ring-[color:var(--accent-violet-ring)]">
          {name}
        </span>
        {domain && (
          <span className="px-1.5 py-0.5 rounded-full text-[10px] shrink-0 bg-[color:var(--accent-slate-bg)] text-[color:var(--accent-slate-fg)] ring-1 ring-inset ring-[color:var(--accent-slate-ring)]">
            {domain}
          </span>
        )}
      </button>
      {open && <Receipt value={entry} />}
    </div>
  );
}

function renderMemoryRecall(result: unknown): ReactNode | null {
  const entries = asRows(result);
  if (!entries) return null;
  return (
    <div className="space-y-0.5">
      {entries.map((e, i) => <MemoryRow key={i} entry={e} />)}
    </div>
  );
}

// ----------------------------------------------------------- task_list

// Tailwind's scanner needs full literal class strings — never template
// the tone into `var(--accent-${tone}-bg)` (memory: colors from OKF source).
const STATUS_PILL: Record<string, string> = {
  pending: "bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)] ring-[color:var(--accent-amber-ring)]",
  in_progress: "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)] ring-[color:var(--accent-teal-ring)]",
  blocked: "bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] ring-[color:var(--accent-rose-ring)]",
  done: "bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)] ring-[color:var(--accent-emerald-ring)]",
};
const STATUS_PILL_DEFAULT =
  "bg-[color:var(--accent-slate-bg)] text-[color:var(--accent-slate-fg)] ring-[color:var(--accent-slate-ring)]";

function TaskRow({ task }: { task: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const status = str(task.status) || "pending";
  const title = str(task.title) || str(task.id) || "(untitled task)";
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-1.5 px-1.5 py-1 text-left rounded hover:bg-[color:var(--surface-2)] transition-colors min-w-0"
      >
        <span
          className={cn(
            "px-1.5 py-0.5 rounded-full text-[9px] uppercase tracking-wider ring-1 ring-inset shrink-0",
            STATUS_PILL[status] ?? STATUS_PILL_DEFAULT,
          )}
        >
          {status.replace(/_/g, " ")}
        </span>
        <span className="text-[11px] text-[color:var(--text-secondary)] truncate">{title}</span>
      </button>
      {open && <Receipt value={task} />}
    </div>
  );
}

function renderTaskList(result: unknown): ReactNode | null {
  const tasks = asRows(result);
  if (!tasks) return null;
  return (
    <div className="space-y-0.5">
      {tasks.map((t, i) => <TaskRow key={i} task={t} />)}
    </div>
  );
}

// ------------------------------------------------------------ registry

export const TOOL_RENDERERS: Map<string, ToolRenderer> = new Map([
  ["brain_search", renderBrainSearch],
  ["memory_recall", renderMemoryRecall],
  ["task_list", renderTaskList],
]);
