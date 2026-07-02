import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "motion/react";
import { type PillTone } from "@/components/ui";
import { SPRING_SNAPPY, DUR, EASE_OUT } from "@/lib/motion";

// Recursive, collapsible task tree (task e72aba6d). PRISM stores a task tree
// via parent_id but every consumer used to look ONE level deep; this renders
// the WHOLE subtree to ANY depth — each parent node gets an expand/collapse
// chevron that reveals its own children, which are themselves TaskTreeNodes.

export type TreeTask = {
  id?: string;
  title?: string;
  status?: string;
  priority?: number | string;
  parent_id?: string;
};

const STATUS_TONE: Record<string, PillTone> = {
  pending: "amber",
  in_progress: "teal",
  blocked: "rose",
  done: "emerald",
};

// Shared done Checkbox — same emerald-fill + spring-tick as the detail page,
// Hermes tokens only (no invented palette).
function Checkbox({ done, reduced }: { done: boolean; reduced: boolean | null }) {
  return (
    <span
      className="inline-flex items-center justify-center h-4 w-4 rounded shrink-0"
      style={{
        background: done ? "var(--accent-emerald-bg)" : "var(--surface-3)",
        boxShadow: `inset 0 0 0 1px var(--accent-${done ? "emerald" : "slate"}-ring)`,
      }}
    >
      <AnimatePresence>
        {done && (
          <motion.svg
            key="tick"
            viewBox="0 0 16 16"
            className="h-3 w-3"
            initial={reduced ? { opacity: 0 } : { scale: 0, opacity: 0 }}
            animate={reduced ? { opacity: 1 } : { scale: 1, opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={reduced ? { duration: 0 } : SPRING_SNAPPY}
          >
            <path d="M3.5 8.5l3 3 6-6.5" fill="none" stroke="var(--accent-emerald-fg)"
              strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </motion.svg>
        )}
      </AnimatePresence>
    </span>
  );
}

// parent_id -> [children] index from a flat task list.
export function buildChildMap(tasks: TreeTask[]): Map<string, TreeTask[]> {
  const m = new Map<string, TreeTask[]>();
  for (const t of tasks) {
    const pid = t.parent_id ?? "";
    if (!pid) continue;
    const arr = m.get(pid);
    if (arr) arr.push(t);
    else m.set(pid, [t]);
  }
  return m;
}

// Whole-subtree descendant count for one node (cycle-guarded via a seen-set).
export function subtreeCount(map: Map<string, TreeTask[]>, id: string): number {
  let n = 0;
  const seen = new Set<string>([id]);
  const queue = [...(map.get(id) ?? [])];
  while (queue.length) {
    const node = queue.shift()!;
    if (!node.id || seen.has(node.id)) continue;
    seen.add(node.id);
    n += 1;
    queue.push(...(map.get(node.id) ?? []));
  }
  return n;
}

function StatusPill({ status }: { status: string }) {
  const tone = STATUS_TONE[status] ?? "slate";
  return (
    <span
      className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0"
      style={{
        background: `var(--accent-${tone}-bg)`,
        color: `var(--accent-${tone}-fg)`,
        boxShadow: `inset 0 0 0 1px var(--accent-${tone}-ring)`,
      }}
    >
      {status}
    </span>
  );
}

// One node + (when expanded) its recursively-rendered children — the recursion
// that makes the tree render to ANY depth.
function TaskTreeNode({
  node, map, depth, rootFrom, reduced,
}: {
  node: TreeTask;
  map: Map<string, TreeTask[]>;
  depth: number;
  rootFrom: string;
  reduced: boolean | null;
}) {
  const navigate = useNavigate();
  const children = node.id ? map.get(node.id) ?? [] : [];
  const hasChildren = children.length > 0;
  // Top level is open by default; deeper levels collapse so a wide tree stays
  // scannable until the operator drills in.
  const [open, setOpen] = useState(depth < 1);
  const count = hasChildren && node.id ? subtreeCount(map, node.id) : 0;

  return (
    <div>
      <div
        className="flex items-center gap-1.5 rounded-md hover:bg-[color:var(--background-base)]/50 transition-colors"
        style={{ paddingLeft: `${depth * 1.15}rem` }}
      >
        {hasChildren ? (
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-label={open ? "collapse" : "expand"}
            className="h-5 w-5 flex items-center justify-center opacity-60 hover:opacity-100 shrink-0"
          >
            <span
              className="inline-block text-[11px] transition-transform"
              style={{ transform: open ? "rotate(90deg)" : "none" }}
            >
              ▸
            </span>
          </button>
        ) : (
          <span className="h-5 w-5 shrink-0" />
        )}
        <button
          type="button"
          onClick={() => node.id && navigate(`/tasks/${node.id}`, { state: { from: `/tasks/${rootFrom}` } })}
          className="flex items-center gap-2 min-w-0 flex-1 text-left py-1.5 pr-2"
        >
          <Checkbox done={(node.status ?? "") === "done"} reduced={reduced} />
          <span className="text-sm truncate">{node.title ?? node.id}</span>
          {hasChildren && (
            <span
              title={`${count} descendant task(s)`}
              className="shrink-0 text-[10px] font-mono leading-none px-1.5 py-0.5 rounded-full"
              style={{
                background: "var(--accent-violet-bg)",
                color: "var(--accent-violet-fg)",
                boxShadow: "inset 0 0 0 1px var(--accent-violet-ring)",
              }}
            >
              {count}
            </span>
          )}
          <span className="ml-auto shrink-0">
            <StatusPill status={node.status ?? "pending"} />
          </span>
        </button>
      </div>
      <AnimatePresence initial={false}>
        {open && hasChildren && (
          <motion.div
            key="kids"
            initial={reduced ? { opacity: 0 } : { opacity: 0, height: 0 }}
            animate={reduced ? { opacity: 1 } : { opacity: 1, height: "auto" }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, height: 0 }}
            transition={{ duration: reduced ? 0 : DUR.enter, ease: EASE_OUT }}
            className="overflow-hidden border-l border-[color:var(--midground-base)]/15 ml-2.5"
          >
            {children.map((c) => (
              <TaskTreeNode
                key={c.id}
                node={c}
                map={map}
                depth={depth + 1}
                rootFrom={rootFrom}
                reduced={reduced}
              />
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// Render the whole subtree rooted at `rootId`, built from a flat task list
// (a single GET /api/tasks the detail page already fetches).
export default function TaskTree({
  tasks, rootId, reduced = false,
}: {
  tasks: TreeTask[];
  rootId: string;
  reduced?: boolean | null;
}) {
  const map = buildChildMap(tasks);
  const roots = map.get(rootId) ?? [];
  if (roots.length === 0) return null;
  return (
    <div className="space-y-0.5 mt-2">
      {roots.map((r) => (
        <TaskTreeNode key={r.id} node={r} map={map} depth={0} rootFrom={rootId} reduced={reduced} />
      ))}
    </div>
  );
}
