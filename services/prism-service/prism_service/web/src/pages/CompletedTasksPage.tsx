import { useEffect, useState, useCallback, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, Empty, Pill, toneFromLabel } from "@/components/ui";

// Completed work lives on its OWN surface — off the active /tasks board so
// finished tasks can never pile up in a Done column again (feedback:
// done-tasks-off-board). Two views over the same data: a time-grouped
// Timeline (glance at what shipped lately) and a Search & filter table
// (look something specific up). Ordered by completed_at — the board's
// priority/created_at sort is exactly why "done" never read as recent.

type Task = {
  id?: string;
  title?: string;
  status?: string;
  priority?: number | string;
  tags?: string[];
  completed_at?: string;
  created_at?: string;
  parent_id?: string;
};

type Row = Task & { when: Date | null };

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function parseWhen(t: Task): Date | null {
  const s = t.completed_at || "";
  if (!s) return null;
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

function fmtDay(d: Date): string {
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
}
function fmtISO(d: Date): string {
  return d.toISOString().slice(0, 10);
}
function dayDiff(d: Date, now: Date): number {
  return Math.floor((now.getTime() - d.getTime()) / 86_400_000);
}

// Relative-time bucket for the Timeline view. Recent work gets named
// buckets; older work falls back to "Month YYYY" so a decade of done
// tasks still groups cleanly instead of scrolling forever.
const RECENT_ORDER = ["Today", "Yesterday", "This week", "Last week", "Earlier this month"];
function bucketOf(d: Date | null, now: Date): string {
  if (!d) return "Undated";
  const diff = dayDiff(d, now);
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff <= 7) return "This week";
  if (diff <= 14) return "Last week";
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()) return "Earlier this month";
  return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

export default function CompletedTasksPage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [view, setView] = useState<"timeline" | "search">("timeline");
  const [query, setQuery] = useState("");
  const [monthOnly, setMonthOnly] = useState(false);
  const [sortKey, setSortKey] = useState<"when" | "title">("when");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);

  const load = useCallback(() => {
    api
      .get<{ tasks: Task[] }>(`/api/tasks?project=${project}`)
      .then((d) => setTasks(d.tasks ?? []))
      .catch(() => setTasks([]))
      .finally(() => setLoaded(true));
  }, [project]);

  useEffect(() => { load(); }, [load]);

  const now = useMemo(() => new Date(), [tasks]);

  // Only DONE root tasks, newest completion first.
  const rows: Row[] = useMemo(() => {
    return tasks
      .filter((t) => (t.status ?? "") === "done" && !t.parent_id)
      .map((t) => ({ ...t, when: parseWhen(t) }))
      .sort((a, b) => (b.when?.getTime() ?? 0) - (a.when?.getTime() ?? 0));
  }, [tasks]);

  const weekCount = useMemo(
    () => rows.filter((r) => r.when && dayDiff(r.when, now) <= 7).length,
    [rows, now],
  );

  const open = (id?: string) => id && navigate(`/tasks/${id}`, { state: { from: "/tasks/completed" } });

  return (
    <Page>
      <div className="flex items-center gap-3">
        <button
          onClick={() => navigate("/tasks")}
          className="text-xs text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)] transition-colors"
        >
          ← Back to board
        </button>
      </div>

      <div className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-semibold text-[color:var(--text-primary)]">Completed</h1>
          <p className="text-sm text-[color:var(--text-secondary)] mt-1 max-w-[60ch]">
            Finished work, off the active board and ordered by when it actually completed.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Pill active={view === "timeline"} onClick={() => setView("timeline")} tone="teal">Timeline</Pill>
          <Pill active={view === "search"} onClick={() => setView("search")} tone="teal">Search &amp; filter</Pill>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <SummaryChip label="total" value={rows.length} />
        <SummaryChip label="this week" value={weekCount} tone="emerald" />
      </div>

      {!loaded ? (
        <Empty>Loading…</Empty>
      ) : rows.length === 0 ? (
        <Empty>No completed tasks yet.</Empty>
      ) : view === "timeline" ? (
        <TimelineView rows={rows} now={now} onOpen={open} />
      ) : (
        <SearchView
          rows={rows}
          query={query}
          setQuery={setQuery}
          monthOnly={monthOnly}
          setMonthOnly={setMonthOnly}
          now={now}
          sortKey={sortKey}
          sortDir={sortDir}
          onSort={(k) => {
            if (k === sortKey) setSortDir((d) => (d === 1 ? -1 : 1));
            else { setSortKey(k); setSortDir(-1); }
          }}
          onOpen={open}
        />
      )}
    </Page>
  );
}

function SummaryChip({ label, value, tone }: { label: string; value: number; tone?: "emerald" }) {
  const fg = tone ? "var(--accent-emerald-fg)" : "var(--text-primary)";
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--surface-2)] px-3 py-1.5 text-xs text-[color:var(--text-muted)]">
      <span className="font-semibold tabular-nums" style={{ color: fg }}>{value}</span> {label}
    </div>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="shrink-0 text-2xs font-mono text-[color:var(--text-muted)] tabular-nums">{children}</span>
  );
}

function Row({ r, onOpen }: { r: Row; onOpen: (id?: string) => void }) {
  return (
    <button
      onClick={() => onOpen(r.id)}
      className="w-full flex items-center gap-3 py-2 px-1 text-left border-b border-[color:var(--border-subtle)] last:border-0 group"
    >
      <span
        className="shrink-0 grid place-items-center w-[18px] h-[18px] rounded-full text-2xs"
        style={{
          background: "var(--accent-sage-bg)",
          color: "var(--accent-sage-fg)",
          boxShadow: "inset 0 0 0 1px var(--accent-sage-ring)",
        }}
      >✓</span>
      <span className="flex-1 min-w-0 truncate text-sm text-[color:var(--text-secondary)] group-hover:text-[color:var(--text-primary)]">
        {r.title ?? "—"}
      </span>
      {(r.tags ?? []).slice(0, 2).map((tag) => (
        <span
          key={tag}
          className="shrink-0 text-2xs font-mono px-1.5 py-0.5 rounded"
          style={{ background: `var(--accent-${toneFromLabel(tag)}-bg)`, color: `var(--accent-${toneFromLabel(tag)}-fg)` }}
        >#{tag}</span>
      ))}
      <Chip>{r.when ? fmtDay(r.when) : "—"}</Chip>
    </button>
  );
}

function TimelineView({ rows, now, onOpen }: { rows: Row[]; now: Date; onOpen: (id?: string) => void }) {
  // Group into ordered buckets. Named recent buckets first (in RECENT_ORDER),
  // then month buckets newest-first, Undated last.
  const groups = new Map<string, Row[]>();
  for (const r of rows) {
    const b = bucketOf(r.when, now);
    (groups.get(b) ?? groups.set(b, []).get(b)!).push(r);
  }
  const keys = [...groups.keys()].sort((a, b) => {
    if (a === "Undated") return 1;
    if (b === "Undated") return -1;
    const ia = RECENT_ORDER.indexOf(a), ib = RECENT_ORDER.indexOf(b);
    if (ia >= 0 || ib >= 0) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    return (groups.get(b)![0].when?.getTime() ?? 0) - (groups.get(a)![0].when?.getTime() ?? 0);
  });

  return (
    <div className="space-y-3">
      {keys.map((k, idx) => {
        const list = groups.get(k)!;
        return (
          <Card key={k} className="!p-0 overflow-hidden">
            <details open={idx < 2}>
              <summary className="flex items-center gap-3 px-4 py-3 cursor-pointer select-none hover:bg-[color:var(--surface-2)] list-none">
                <span className="text-[color:var(--text-muted)] text-xs transition-transform [details[open]_&]:rotate-90">▶</span>
                <span className="font-medium text-sm text-[color:var(--text-primary)]">{k}</span>
                <span className="ml-auto text-xs text-[color:var(--text-muted)] tabular-nums">{list.length} done</span>
              </summary>
              <div className="px-4 pb-2 border-t border-[color:var(--border-subtle)]">
                {list.map((r) => <Row key={r.id} r={r} onOpen={onOpen} />)}
              </div>
            </details>
          </Card>
        );
      })}
    </div>
  );
}

function SearchView({
  rows, query, setQuery, monthOnly, setMonthOnly, now, sortKey, sortDir, onSort, onOpen,
}: {
  rows: Row[];
  query: string; setQuery: (s: string) => void;
  monthOnly: boolean; setMonthOnly: (b: boolean) => void;
  now: Date;
  sortKey: "when" | "title"; sortDir: 1 | -1;
  onSort: (k: "when" | "title") => void;
  onOpen: (id?: string) => void;
}) {
  const q = query.trim().toLowerCase();
  let filtered = rows.filter((r) => {
    if (q && !(r.title ?? "").toLowerCase().includes(q)) return false;
    if (monthOnly && !(r.when && dayDiff(r.when, now) <= 30)) return false;
    return true;
  });
  filtered = [...filtered].sort((a, b) => {
    let v = 0;
    if (sortKey === "when") v = (a.when?.getTime() ?? 0) - (b.when?.getTime() ?? 0);
    else v = (a.title ?? "").localeCompare(b.title ?? "");
    return v * sortDir;
  });
  const arrow = (k: "when" | "title") => (sortKey === k ? (sortDir < 0 ? " ↓" : " ↑") : "");

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex-1 min-w-[220px] flex items-center gap-2 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] px-3 py-2">
          <span className="text-[color:var(--text-muted)]">⌕</span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`Search ${rows.length} completed tasks…`}
            className="flex-1 bg-transparent outline-none text-sm text-[color:var(--text-primary)] placeholder:text-[color:var(--text-disabled)]"
          />
        </div>
        <Pill active={monthOnly} onClick={() => setMonthOnly(!monthOnly)} tone="teal">This month</Pill>
      </div>

      <Card className="!p-0 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr>
                <th
                  onClick={() => onSort("title")}
                  className="text-left text-2xs uppercase tracking-wider text-[color:var(--text-label)] font-semibold px-4 py-2.5 border-b border-[color:var(--border-default)] cursor-pointer hover:text-[color:var(--text-secondary)] select-none"
                >Task{arrow("title")}</th>
                <th
                  onClick={() => onSort("when")}
                  className="text-left text-2xs uppercase tracking-wider text-[color:var(--text-label)] font-semibold px-4 py-2.5 border-b border-[color:var(--border-default)] cursor-pointer hover:text-[color:var(--text-secondary)] select-none whitespace-nowrap w-px"
                >Completed{arrow("when")}</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr><td colSpan={2} className="px-4 py-8 text-center text-sm text-[color:var(--text-muted)]">No completed tasks match.</td></tr>
              ) : (
                filtered.map((r) => (
                  <tr
                    key={r.id}
                    onClick={() => onOpen(r.id)}
                    className="cursor-pointer hover:bg-[color:var(--surface-2)]"
                  >
                    <td className="px-4 py-2 border-b border-[color:var(--border-subtle)] text-[color:var(--text-primary)] max-w-0 truncate">{r.title ?? "—"}</td>
                    <td className="px-4 py-2 border-b border-[color:var(--border-subtle)] font-mono text-[color:var(--text-muted)] tabular-nums whitespace-nowrap">{r.when ? fmtISO(r.when) : "—"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
      <div className="text-xs text-[color:var(--text-muted)] tabular-nums">
        Showing {filtered.length} of {rows.length} completed
      </div>
    </div>
  );
}
