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
  if (status === "done") return "done";
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

export default function TasksPage() {
  const [project] = useProject();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [next, setNext] = useState<Task | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

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
    if (t.parent_id) {
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
              <th className="text-left text-2xs uppercase tracking-wider font-semibold px-3 py-2 border-b border-[color:var(--border-default)]" style={{ color: "var(--text-muted)" }}>Summary</th>
              <th className="text-left text-2xs uppercase tracking-wider font-semibold px-3 py-2 w-32 border-b border-[color:var(--border-default)]" style={{ color: "var(--text-muted)" }}>Who</th>
              <th className="text-right text-2xs uppercase tracking-wider font-semibold px-3 py-2 w-16 border-b border-[color:var(--border-default)]" style={{ color: "var(--text-muted)" }}>Prio</th>
              <th className="text-right text-2xs uppercase tracking-wider font-semibold px-3 py-2 w-20 border-b border-[color:var(--border-default)]" style={{ color: "var(--text-muted)" }}>Updated</th>
            </tr>
          </thead>
          <tbody>
            {GROUPS.map((g) => {
              const items = roots.filter((t) => bucketOf(t) === g.key);
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
  label, items, childrenByParent, expanded, toggle, nextId,
}: {
  label: string;
  items: Task[];
  childrenByParent: Map<string, Task[]>;
  expanded: Set<string>;
  toggle: (id: string) => void;
  nextId?: string;
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
        const kids = childrenByParent.get(t.id ?? "") ?? [];
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
