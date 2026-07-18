import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page } from "@/components/ui";
import { stepLabel } from "@/lib/workflowChips";
import { Lozenge } from "@/components/Lozenge";

type Task = {
  id?: string;
  title?: string;
  status?: string;
  priority?: number | string;
  assigned_agent?: string;
  workflow_step?: string;
  gate_state?: string;
  gate_reason?: string;
  parent_id?: string;
  updated_at?: string;
};

// Status buckets become the Jira-style group rows. Order is the reading order
// of the board: what's moving, what's blocking a human at a gate, what's queued,
// what's stuck. There is deliberately no "done" group — completed work leaves
// the active board (feedback: done-tasks-off-board) and lives behind the
// Completed link-row at the bottom.
const GROUPS: { key: string; label: string }[] = [
  { key: "in_progress", label: "In progress" },
  { key: "gate", label: "At a gate" },
  { key: "pending", label: "Up next" },
  { key: "blocked", label: "Blocked" },
];

function bucketOf(t: Task): string {
  const status = (t.status ?? "pending").toLowerCase();
  // Terminal / soft-deleted statuses NEVER reach an active bucket. They
  // used to fall through to "Up next" — and, with a stale gate_state, even
  // "At a gate" — so the board presented weeks of cancelled/deleted history
  // as upcoming work (owner 2026-07-17: "tons of subtasks in there?").
  if (status === "done") return "done";
  if (status === "cancelled" || status === "deleted" || status === "archived")
    return "hidden";
  if (status === "blocked") return "blocked";
  const gate = t.gate_state ?? "none";
  if (gate === "pending" || gate === "failed") return "gate";
  if (status === "in_progress") return "in_progress";
  return "pending";
}

const shortId = (id?: string) => (id ?? "").slice(0, 8) || "—";

// Compact relative age for the tabular Updated column ("2h", "1d", "now").
function relTime(iso?: string): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 604800) return `${Math.floor(s / 86400)}d`;
  return `${Math.floor(s / 604800)}w`;
}

function gateTone(gate: string): "warn" | "danger" | "ok" | null {
  if (gate === "pending") return "warn";
  if (gate === "failed") return "danger";
  if (gate === "passed") return "ok";
  return null;
}

// Column sorting. The board stays grouped by status bucket; sorting reorders
// rows WITHIN each group (and each expanded child list), so the reading order
// of the buckets is never lost. `null` sort = incoming API order (the default).
type SortKey = "summary" | "who" | "prio" | "updated";
type SortDir = "asc" | "desc";
type Sort = { key: SortKey; dir: SortDir };

function cmpTasks(a: Task, b: Task, key: SortKey): number {
  switch (key) {
    case "summary":
      return (a.title ?? "").localeCompare(b.title ?? "");
    case "who":
      // Unassigned rows sort after assigned ones (ascending).
      return (a.assigned_agent ?? "").localeCompare(b.assigned_agent ?? "");
    case "prio": {
      const pa = Number(a.priority ?? 0);
      const pb = Number(b.priority ?? 0);
      return (Number.isNaN(pa) ? 0 : pa) - (Number.isNaN(pb) ? 0 : pb);
    }
    case "updated":
      return (Date.parse(a.updated_at ?? "") || 0) - (Date.parse(b.updated_at ?? "") || 0);
  }
}

// Text columns default to A→Z; numeric/time columns default to high→recent
// first, matching how a queue reads. Re-clicking the active column flips it.
function defaultDir(key: SortKey): SortDir {
  return key === "summary" || key === "who" ? "asc" : "desc";
}

export default function TasksPage() {
  const [project] = useProject();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [next, setNext] = useState<Task | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [sort, setSort] = useState<Sort | null>(null);

  const load = useCallback(() => {
    api.get<{ tasks: Task[] }>(`/api/tasks?project=${project}`)
      .then((d) => setTasks(d.tasks))
      .catch(() => setTasks([]));
    api.get<{ next: Task | null }>(`/api/tasks/next?project=${project}`).then((d) => setNext(d.next)).catch(() => setNext(null));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  // Hierarchy: the board lists root tasks; a root with children gets an
  // expand twist that reveals the epic → workstream tree inline. The list API
  // returns every task (no parent_id filter — see api/tasks.list_tasks), so we
  // group client-side.
  const childrenByParent = new Map<string, Task[]>();
  for (const t of tasks) {
    // Cancelled/deleted children stay off the board's expansion tree — the
    // task detail page keeps the full history; the board shows live work.
    const cst = (t.status ?? "").toLowerCase();
    if (t.parent_id && cst !== "cancelled" && cst !== "deleted") {
      const arr = childrenByParent.get(t.parent_id) ?? [];
      arr.push(t);
      childrenByParent.set(t.parent_id, arr);
    }
  }
  const roots = tasks.filter((t) => !t.parent_id);
  const doneCount = roots.filter((t) => (t.status ?? "").toLowerCase() === "done").length;
  const nextId = next?.id;

  const toggle = (id: string) => setExpanded((prev) => {
    const nn = new Set(prev);
    nn.has(id) ? nn.delete(id) : nn.add(id);
    return nn;
  });

  // Clicking a header: activate that column (with its natural default
  // direction) or, if already active, flip the direction.
  const clickSort = (key: SortKey) => setSort((prev) =>
    prev && prev.key === key
      ? { key, dir: prev.dir === "asc" ? "desc" : "asc" }
      : { key, dir: defaultDir(key) });

  const sorter = useCallback((list: Task[]): Task[] => {
    if (!sort) return list;
    const arr = [...list];
    arr.sort((a, b) => {
      const c = cmpTasks(a, b, sort.key);
      return sort.dir === "asc" ? c : -c;
    });
    return arr;
  }, [sort]);

  return (
    <Page>
      {/* The conductor pulse (LIVE bar) now lives in the app shell (App.tsx →
          LiveBar) so it persists on every page, not just the board. */}

      {/* Grouped Jira-style board. Group rows partition by status bucket; each
          task row carries its short id, clickable summary, step + gate lozenges,
          who, priority, and a tabular age. Root tasks with children expand into
          their workstream tree. */}
      <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr>
              <SortHeader label="Summary" col="summary" sort={sort} onSort={clickSort} align="left" />
              <SortHeader label="Who" col="who" sort={sort} onSort={clickSort} align="left" width="w-32" />
              <SortHeader label="Prio" col="prio" sort={sort} onSort={clickSort} align="right" width="w-16" />
              <SortHeader label="Updated" col="updated" sort={sort} onSort={clickSort} align="right" width="w-20" />
            </tr>
          </thead>
          <tbody>
            {GROUPS.map((g) => {
              const items = sorter(roots.filter((t) => bucketOf(t) === g.key));
              if (items.length === 0) return null;
              return (
                <GroupBlock
                  key={g.key}
                  label={g.label}
                  items={items}
                  childrenByParent={childrenByParent}
                  expanded={expanded}
                  toggle={toggle}
                  nextId={nextId}
                  sorter={sorter}
                />
              );
            })}
            {roots.length === 0 && (
              <tr><td colSpan={4} className="px-3 py-8 text-center text-xs" style={{ color: "var(--text-muted)" }}>Queue is clear.</td></tr>
            )}
          </tbody>
        </table>
        {/* Completed leaves the active board — a link-row, not a group. */}
        <Link
          to="/tasks/completed"
          className="flex items-center gap-2 px-3 py-2.5 border-t border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] transition-colors"
          style={{ background: "var(--surface-1)" }}
        >
          <span className="text-2xs uppercase tracking-wider font-semibold" style={{ color: "var(--text-secondary)" }}>Completed</span>
          <span className="text-xs tabular-nums" style={{ color: "var(--text-muted)" }}>· {doneCount}</span>
          <span className="ml-auto text-xs" style={{ color: "var(--accent-teal-fg)" }}>→</span>
        </Link>
      </div>
    </Page>
  );
}

function GroupBlock({
  label, items, childrenByParent, expanded, toggle, nextId, sorter,
}: {
  label: string;
  items: Task[];
  childrenByParent: Map<string, Task[]>;
  expanded: Set<string>;
  toggle: (id: string) => void;
  nextId?: string;
  sorter: (list: Task[]) => Task[];
}) {
  return (
    <>
      <tr>
        <td colSpan={4} className="px-3 py-1.5 bg-[color:var(--surface-2)] border-b border-[color:var(--border-subtle)]">
          <span className="text-xs font-semibold" style={{ color: "var(--text-primary)" }}>{label}</span>
          <span className="text-2xs ml-2 tabular-nums" style={{ color: "var(--text-muted)" }}>· {items.length}</span>
        </td>
      </tr>
      {items.map((t) => {
        const kids = sorter(childrenByParent.get(t.id ?? "") ?? []);
        const isOpen = expanded.has(t.id ?? "");
        return (
          <TaskRows
            key={t.id}
            task={t}
            kids={kids}
            isOpen={isOpen}
            toggle={toggle}
            isNext={t.id === nextId}
          />
        );
      })}
    </>
  );
}

function TaskRows({ task, kids, isOpen, toggle, isNext }: {
  task: Task; kids: Task[]; isOpen: boolean; toggle: (id: string) => void; isNext: boolean;
}) {
  return (
    <>
      <TaskRow task={task} depth={0} hasChildren={kids.length > 0} isOpen={isOpen} toggle={toggle} isNext={isNext} />
      {isOpen && kids.map((c) => (
        <TaskRow key={c.id} task={c} depth={1} hasChildren={false} isOpen={false} toggle={toggle} isNext={false} />
      ))}
    </>
  );
}

function TaskRow({ task, depth, hasChildren, isOpen, toggle, isNext }: {
  task: Task; depth: number; hasChildren: boolean; isOpen: boolean; toggle: (id: string) => void; isNext: boolean;
}) {
  const step = task.workflow_step ?? "";
  const gate = task.gate_state ?? "none";
  const gTone = gateTone(gate);
  return (
    <tr className="h-10 hover:bg-[color:var(--surface-2)] transition-colors border-b border-[color:var(--border-subtle)]">
      <td className="px-3 py-1.5">
        <div className="flex items-center gap-2 min-w-0" style={{ paddingLeft: depth * 22 }}>
          {hasChildren ? (
            <button
              type="button"
              onClick={() => task.id && toggle(task.id)}
              className="shrink-0 w-4 text-2xs font-mono leading-none"
              style={{ color: "var(--text-muted)" }}
              aria-label={isOpen ? "collapse" : "expand"}
            >
              {isOpen ? "▾" : "▸"}
            </button>
          ) : (
            <span className="shrink-0 w-4" />
          )}
          <span className="shrink-0 font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }}>{shortId(task.id)}</span>
          <Link
            to={`/tasks/${task.id}`}
            state={{ from: "/tasks" }}
            className="truncate font-medium hover:underline decoration-dotted underline-offset-2"
            style={{ color: "var(--text-primary)" }}
          >
            {task.title ?? "—"}
          </Link>
          {isNext && <Lozenge tone="new">next</Lozenge>}
          {step && <Lozenge tone="info">{stepLabel(step)}</Lozenge>}
          {gTone && <Lozenge tone={gTone}>{`gate ${gate}`}</Lozenge>}
        </div>
      </td>
      <td className="px-3 py-1.5">
        {task.assigned_agent
          ? <span className="font-mono text-2xs truncate block" style={{ color: "var(--text-secondary)" }}>{task.assigned_agent}</span>
          : <span className="text-2xs" style={{ color: "var(--text-disabled)" }}>—</span>}
      </td>
      <td className="px-3 py-1.5 text-right">
        {typeof task.priority !== "undefined"
          ? <span className="font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }}>p{task.priority}</span>
          : <span className="text-2xs" style={{ color: "var(--text-disabled)" }}>—</span>}
      </td>
      <td className="px-3 py-1.5 text-right font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }}>
        {relTime(task.updated_at)}
      </td>
    </tr>
  );
}

// Clickable column header. Shows a caret on the active sort column; the whole
// header is the hit target. Alignment/width match the original static headers.
function SortHeader({ label, col, sort, onSort, align, width }: {
  label: string;
  col: SortKey;
  sort: Sort | null;
  onSort: (key: SortKey) => void;
  align: "left" | "right";
  width?: string;
}) {
  const active = sort?.key === col;
  const caret = active ? (sort!.dir === "asc" ? "▲" : "▼") : "";
  const alignClass = align === "right" ? "text-right" : "text-left";
  return (
    <th
      className={`${alignClass} text-2xs uppercase tracking-wider font-semibold px-3 py-2 border-b border-[color:var(--border-default)] cursor-pointer select-none${width ? ` ${width}` : ""}`}
      style={{ color: active ? "var(--text-secondary)" : "var(--text-muted)" }}
      onClick={() => onSort(col)}
      aria-sort={active ? (sort!.dir === "asc" ? "ascending" : "descending") : "none"}
    >
      <span className={`inline-flex items-center gap-1 ${align === "right" ? "flex-row-reverse" : ""}`}>
        <span className="hover:underline decoration-dotted underline-offset-2">{label}</span>
        <span className="text-[9px] w-2 leading-none" style={{ color: "var(--accent-teal-fg)" }}>{caret}</span>
      </span>
    </th>
  );
}
