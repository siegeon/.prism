import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  api,
  listWorkspaces,
  listIntegrationEntities,
  type ExternalEntity,
} from "@/lib/api";
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
  parent_id?: string;
  updated_at?: string;
  tags?: string[];
  gate_state?: string;
  mirrors?: TaskMirror[];
};

// One badge-worth of data per active provider link (api/tasks.py `mirrors`,
// task 6fbbec35 — plural, store-derived; supersedes the singular
// description-prose `mirror_url`).
type TaskMirror = { provider: string; issue?: string; url: string; last_synced_at?: string };

// A row on the unified Work surface — a native PRISM task OR an external
// GitHub/Jira item, normalized to one shape so My Work/Team, the filters, and
// the keyboard cursor treat every provider identically.
type WorkSource = "native" | "github" | "jira";
type WorkItem = {
  key: string;              // stable react key + cursor id
  source: WorkSource;
  id?: string;              // local task id (native, or an imported item's link)
  title: string;
  assignee: string;
  status: string;           // local bucket status (pending/in_progress/...)
  workflow_step?: string;
  gate_state?: string;
  priority?: number | string;
  updated_at?: string;
  parent_id?: string;
  // external-only
  remoteStatus?: string;    // raw provider status — NEVER a local status
  url?: string;             // backlink to the provider
  displayKey?: string;      // e.g. #123 / PROJ-1
  restricted?: boolean;     // server says: exists, but linked context is hidden
  imported?: boolean;       // has a local intake task that can be started
  tags?: string[];          // provenance, e.g. ['github','external']
  mirrors?: TaskMirror[];   // server-derived, store-backed backlinks (0..n)
};

// My Work vs Team — the attention model. "mine" scopes to the signed-in
// viewer's assigned rows; "team" shows the whole team's work.
type WorkView = "mine" | "team";

const shortId = (id?: string) => (id ?? "").slice(0, 8) || "—";

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


function providerOf(kind?: string, provider?: string): WorkSource {
  if (provider === "github" || provider === "jira") return provider;
  if (kind === "jira_issue") return "jira";
  if (kind === "issue" || kind === "pull_request") return "github";
  return "github";
}

// Everything on this page IS a PRISM task, because PRISM is where the work
// happens, so labelling every row "PRISM" says nothing (owner 2026-07-28).
// A badge earns its place only by taking you somewhere: a task mirrored from
// a provider gets a link to the real thing, and a native task gets nothing.
// The URL(s) are derived SERVER-SIDE from the integration STORE (api/tasks.py
// `mirrors` field, task 6fbbec35 — supersedes the singular description-prose
// `mirror_url`, task 842248bd) so a task linked to BOTH github and jira
// renders both badges, not just the first.

function mirrorsOf(item: WorkItem): { label: string; href: string }[] {
  if (item.url) {
    return [{ label: item.source === "jira" ? "JIRA" : "GITHUB", href: item.url }];
  }
  // A native task's counterparts are whatever the store says is ACTIVE —
  // never description prose, never capped to one provider.
  return (item.mirrors ?? []).map((m) => ({
    label: m.provider === "jira" ? "JIRA" : m.provider.toUpperCase(),
    href: m.url,
  }));
}

function nativeToWork(t: Task): WorkItem {
  return {
    key: `native:${t.id}`,
    source: "native",
    id: t.id,
    title: t.title ?? "—",
    assignee: t.assigned_agent ?? "",
    status: (t.status ?? "pending").toLowerCase(),
    workflow_step: t.workflow_step,
    gate_state: t.gate_state,
    priority: t.priority,
    updated_at: t.updated_at,
    tags: t.tags,
    mirrors: t.mirrors,
    parent_id: t.parent_id,
  };
}

function externalToWork(e: ExternalEntity): WorkItem {
  const source = providerOf(e.entity_kind, e.provider);
  return {
    key: `ext:${e.id}`,
    source,
    id: e.task_id,
    title: e.title || e.display_key || e.id,
    assignee: (e.assignees && e.assignees[0]) || "",
    status: "pending",
    updated_at: e.remote_updated_at || e.last_seen_at,
    remoteStatus: e.remote_status || e.status_category || "",
    url: e.url,
    displayKey: e.display_key,
    restricted: !!e.restricted,
    imported: !!e.task_id,
  };
}

// A single common key so native + external rows interleave into ONE ordered
// queue instead of rendering as two stacked blocks (native-then-external).
// Missing/unparseable timestamps sort last rather than first.
function workItemTimestamp(it: WorkItem): number {
  const t = it.updated_at ? Date.parse(it.updated_at) : NaN;
  return Number.isNaN(t) ? -Infinity : t;
}

export default function TasksPage() {
  const [project] = useProject();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [external, setExternal] = useState<ExternalEntity[]>([]);
  const [me, setMe] = useState<string>("");
  const [view, setView] = useState<WorkView>("team");
  const [assigneeFilter, setAssigneeFilter] = useState<string>("");
  // Owner 2026-07-29: "i need a way to search work for e696d952 a task on
  // this" — id prefix, full uuid, title, or tag, case-insensitive.
  const [searchParams] = useSearchParams();
  const [query, setQuery] = useState<string>("");
  useEffect(() => {                       // seed from ?q= (task 1c9899d6)
    const q = searchParams.get("q");
    if (q) setQuery(q);
  }, [searchParams]);
  const [cursor, setCursor] = useState<number>(0);
  const [started, setStarted] = useState<Set<string>>(new Set());
  const listRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(() => {
    // LEAN board projection (task 842248bd): the unprojected board shipped
    // 1.84 MB over 356 rows, 86% of it fields this page never renders.
    // `description` is deliberately NOT in this set — the server derives
    // `mirrors` instead so the link-out badge(s) survive without the raw
    // body riding the board (see mirrorsOf below).
    api.get<{ tasks: Task[] }>(`/api/tasks?project=${project}&fields=id,title,status,assigned_agent,priority,updated_at,workflow_step,gate_state,parent_id,tags,mirrors`)
      .then((d) => setTasks(d.tasks))
      .catch(() => setTasks([]));
    // The viewer identity powers My Work; failure just leaves it empty (Team
    // still works). Authorization is the server's — we never infer it here.
    api.get<{ user?: { id?: string; display_name?: string; email?: string } }>("/api/auth/me")
      .then((d) => setMe(d.user?.display_name || d.user?.id || d.user?.email || ""))
      .catch(() => setMe(""));
    // Merge external GitHub/Jira work from the first workspace the viewer can
    // see. Best-effort: no workspace / no integrations -> native-only board.
    listWorkspaces()
      .then(async (workspaces) => {
        if (!workspaces.length) { setExternal([]); return; }
        const rows = await listIntegrationEntities(workspaces[0].id);
        setExternal(rows);
      })
      .catch(() => setExternal([]));
  }, [project]);

  // Only poll a tab someone is looking at (Sidebar useStaleness precedent);
  // refetch on focus so the board is current the moment it is seen.
  useEffect(() => {
    const tick = () => { if (!document.hidden) load(); };
    load();
    const t = setInterval(tick, 5000);
    document.addEventListener("visibilitychange", tick);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", tick); };
  }, [load]);

  // The unified, filtered, viewer-scoped work list — ONE queue, not two.
  const items = useMemo(() => {
    const nativeRows = tasks.filter((t) => {
      const s = (t.status ?? "").toLowerCase();
      return !t.parent_id && s !== "done" && s !== "cancelled" && s !== "deleted" && s !== "archived";
    }).map(nativeToWork);
    // An external entity already imported into a rendered native task is the
    // SAME piece of work as that native row — drop it here so it doesn't
    // render a second time (owner report: "two separate work queues").
    const renderedNativeIds = new Set(nativeRows.map((t) => t.id).filter(Boolean));
    const externalRows = external
      .filter((e) => !(e.task_id && renderedNativeIds.has(e.task_id)))
      .map(externalToWork);
    // Sort by a common key so native + external interleave into one ordered
    // queue instead of two stacked blocks (native-then-external).
    const merged: WorkItem[] = [...nativeRows, ...externalRows]
      .sort((a, b) => workItemTimestamp(b) - workItemTimestamp(a));
    const q = query.trim().toLowerCase();
    return merged.filter((it) => {
      if (assigneeFilter && !it.assignee.toLowerCase().includes(assigneeFilter.toLowerCase())) return false;
      if (q) {
        const idHit = it.id?.toLowerCase().startsWith(q) || it.id?.toLowerCase().includes(q);
        const titleHit = it.title.toLowerCase().includes(q);
        const tagHit = (it.tags ?? []).some((t) => t.toLowerCase().includes(q));
        if (!idHit && !titleHit && !tagHit) return false;
      }
      if (view === "mine") {
        // My Work: rows assigned to the signed-in viewer. With no identity we
        // fall back to "assigned to someone" so the toggle still narrows.
        if (me) return it.assignee.toLowerCase() === me.toLowerCase();
        return it.assignee !== "";
      }
      return true;
    });
  }, [tasks, external, assigneeFilter, view, me, query]);

  // Keyboard navigation across the unified rows: j/ArrowDown and k/ArrowUp move
  // the cursor; Enter opens the focused row's local task.
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      const tag = (ev.target as HTMLElement | null)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (ev.key === "j" || ev.key === "ArrowDown") {
        setCursor((c) => Math.min(items.length - 1, c + 1)); ev.preventDefault();
      } else if (ev.key === "k" || ev.key === "ArrowUp") {
        setCursor((c) => Math.max(0, c - 1)); ev.preventDefault();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items.length]);

  const assignees = useMemo(() => {
    const s = new Set<string>();
    for (const it of [...tasks.map(nativeToWork), ...external.map(externalToWork)]) {
      if (it.assignee) s.add(it.assignee);
    }
    return [...s].sort();
  }, [tasks, external]);

  const start = useCallback((it: WorkItem) => {
    if (!it.id) return;
    setStarted((prev) => new Set(prev).add(it.key));
    // Intake -> a normal local workflow: link a session then advance. The
    // server owns the transition; we only kick it off from the imported item.
    api.post(`/api/tasks/${it.id}/conductor/work?project=${project}`, {}).catch(() => {});
  }, [project]);

  return (
    <Page>
      {/* Work-surface control bar: My Work / Team attention toggle + provider
          and assignee filters spanning native / GitHub / Jira. */}
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <div className="inline-flex rounded-md border border-[color:var(--border-default)] overflow-hidden" role="tablist" aria-label="Work view">
          {(["mine", "team"] as WorkView[]).map((v) => (
            <button
              key={v}
              type="button"
              role="tab"
              data-work-view={v}
              aria-selected={view === v}
              onClick={() => setView(v)}
              className="px-3 py-1.5 text-xs font-semibold"
              style={{
                background: view === v ? "var(--surface-2)" : "var(--surface-1)",
                color: view === v ? "var(--text-primary)" : "var(--text-muted)",
              }}
            >
              {v === "mine" ? "My Work" : "Team"}
            </button>
          ))}
        </div>

        <input
          data-work-search
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search id, title, tag..."
          aria-label="Search work by id, title, or tag"
          className="px-2 py-1.5 text-xs rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] min-w-0"
          style={{ color: "var(--text-secondary)" }}
        />

        <select
          data-assignee-filter
          value={assigneeFilter}
          onChange={(e) => setAssigneeFilter(e.target.value)}
          className="px-2 py-1.5 text-xs rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)]"
          style={{ color: "var(--text-secondary)" }}
          aria-label="Filter by assignee"
        >
          <option value="">All assignees</option>
          {assignees.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>

        <span className="ml-auto text-2xs tabular-nums" style={{ color: "var(--text-muted)" }}>
          {items.length} item{items.length === 1 ? "" : "s"}
        </span>
      </div>

      <div ref={listRef} className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] overflow-x-auto">
        <table className="w-full table-fixed border-collapse text-sm">
          <thead>
            <tr>
              <th className="text-left text-2xs uppercase tracking-wider font-semibold px-3 py-2 border-b border-[color:var(--border-default)]" style={{ color: "var(--text-muted)" }}>Summary</th>
              <th className="text-left text-2xs uppercase tracking-wider font-semibold px-3 py-2 border-b border-[color:var(--border-default)] w-24" style={{ color: "var(--text-muted)" }}>Status</th>
              <th className="text-left text-2xs uppercase tracking-wider font-semibold px-3 py-2 border-b border-[color:var(--border-default)] w-32" style={{ color: "var(--text-muted)" }}>Who</th>
              <th className="text-right text-2xs uppercase tracking-wider font-semibold px-3 py-2 border-b border-[color:var(--border-default)] w-16" style={{ color: "var(--text-muted)" }}>Prio</th>
              <th className="text-right text-2xs uppercase tracking-wider font-semibold px-3 py-2 border-b border-[color:var(--border-default)] w-20" style={{ color: "var(--text-muted)" }}>Updated</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it, i) => (
              <WorkRow
                key={it.key}
                item={it}
                focused={i === cursor}
                started={started.has(it.key)}
                onStart={() => start(it)}
              />
            ))}
            {items.length === 0 && (
              <tr><td colSpan={5} className="px-3 py-8 text-center text-xs" style={{ color: "var(--text-muted)" }}>No work in this view.</td></tr>
            )}
          </tbody>
        </table>
        <Link
          to="/tasks/completed"
          className="flex items-center gap-2 px-3 py-2.5 border-t border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] transition-colors"
          style={{ background: "var(--surface-1)" }}
        >
          <span className="text-2xs uppercase tracking-wider font-semibold" style={{ color: "var(--text-secondary)" }}>Completed</span>
          <span className="ml-auto text-xs" style={{ color: "var(--accent-teal-fg)" }}>→</span>
        </Link>
      </div>
    </Page>
  );
}

function WorkRow({ item, focused, started, onStart }: {
  item: WorkItem; focused: boolean; started: boolean; onStart: () => void;
}) {
  const navigate = useNavigate();
  const step = item.workflow_step ?? "";
  const gate = item.gate_state ?? "none";
  const gTone = gateTone(gate);
  const external = item.source !== "native";
  // The WHOLE row is clickable through to the task (owner: "when i click on
  // a task in the work view, it will navigate to the item"), not just the
  // title text — same precedent LiveBar's own rows already set (owner
  // 2026-07-16: "each one of the items on the line is a task and needs to
  // be able to be clicked through"). Interactive sub-elements (the provider
  // mirror link, the Start button) stop propagation so they act on
  // themselves instead of also navigating.
  const openable = !!item.id && !item.restricted;
  const open = () => { if (openable) navigate(`/tasks/${item.id}`, { state: { from: "/tasks" } }); };
  return (
    <tr
      className="h-10 transition-colors border-b border-[color:var(--border-subtle)]"
      style={{ background: focused ? "var(--surface-2)" : undefined, cursor: openable ? "pointer" : undefined }}
      aria-selected={focused}
      onClick={open}
    >
      <td className="px-3 py-1.5">
        <div className="flex items-center gap-2 min-w-0">
          {mirrorsOf(item).map((mirror) => (
            <a
              key={mirror.label + mirror.href}
              href={mirror.href}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="shrink-0 text-2xs font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)]"
              style={{ color: "var(--accent-teal-fg)" }}
              aria-label={`Open this task's ${mirror.label} issue`}
            >
              {mirror.label}
            </a>
          ))}
          <span className="shrink-0 font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }}>
            {item.displayKey ?? shortId(item.id)}
          </span>
          {item.restricted ? (
            // The row exists, but the viewer isn't authorized for its linked
            // context — a placeholder, never the hidden metadata.
            <span data-restricted className="italic text-2xs" style={{ color: "var(--text-disabled)" }}>
              Restricted — you don't have access to this linked item
            </span>
          ) : item.id ? (
            <Link
              to={`/tasks/${item.id}`}
              state={{ from: "/tasks" }}
              onClick={(e) => e.stopPropagation()}
              className="truncate min-w-0 font-medium hover:underline decoration-dotted underline-offset-2"
              style={{ color: "var(--text-primary)" }}
            >
              {item.title}
            </Link>
          ) : (
            <span className="truncate min-w-0 font-medium" style={{ color: "var(--text-primary)" }}>{item.title}</span>
          )}
          {step && <Lozenge tone="info">{stepLabel(step)}</Lozenge>}
          {gTone && <Lozenge tone={gTone}>{`gate ${gate}`}</Lozenge>}
        </div>
      </td>
      <td className="px-3 py-1.5">
        {external ? (
          // Remote status is the provider's truth and is labelled as such so it
          // is never confused with the local conductor status.
          <span className="text-2xs" style={{ color: "var(--text-muted)" }} title="provider status">
            Remote: {item.remoteStatus || "—"}
          </span>
        ) : (
          <span className="text-2xs" style={{ color: "var(--text-secondary)" }}>{item.status}</span>
        )}
      </td>
      <td className="px-3 py-1.5">
        {item.assignee
          ? <span className="font-mono text-2xs truncate block" style={{ color: "var(--text-secondary)" }}>{item.assignee}</span>
          : <span className="text-2xs" style={{ color: "var(--text-disabled)" }}>—</span>}
      </td>
      <td className="px-3 py-1.5 text-right">
        {external && !item.restricted ? (
          started ? (
            <span className="text-2xs" style={{ color: "var(--text-muted)" }}>started</span>
          ) : (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onStart(); }}
              className="text-2xs font-semibold px-2 py-0.5 rounded border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)]"
              style={{ color: "var(--accent-teal-fg)" }}
            >
              Start
            </button>
          )
        ) : typeof item.priority !== "undefined" ? (
          <span className="font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }}>p{item.priority}</span>
        ) : <span className="text-2xs" style={{ color: "var(--text-disabled)" }}>—</span>}
      </td>
      <td className="px-3 py-1.5 text-right font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }}>
        {relTime(item.updated_at)}
      </td>
    </tr>
  );
}
