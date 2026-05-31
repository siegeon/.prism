import { useEffect, useState, useCallback } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, toneFromLabel, type PillTone } from "@/components/ui";
import {
  stepChipClass, gateChipClass, gateLabel, stepLabel,
} from "@/lib/workflowChips";

// Same status → tone map as TasksPage so the detail-page status chip
// matches the kanban column header it came from.
const STATUS_TONE: Record<string, PillTone> = {
  pending: "amber",
  in_progress: "teal",
  blocked: "rose",
  done: "emerald",
};

function priorityTone(p: number | string | undefined): PillTone {
  if (p === undefined || p === null) return "slate";
  const n = typeof p === "number" ? p : Number(p);
  if (!Number.isFinite(n)) return toneFromLabel(String(p));
  if (n <= 1) return "rose";
  if (n === 2) return "amber";
  if (n === 3) return "sage";
  if (n === 4) return "violet";
  return "slate";
}

type Task = {
  id?: string;
  title?: string;
  status?: string;
  priority?: number | string;
  tags?: string[];
  assigned_agent?: string;
  description?: string;
  story_file?: string;
  created_at?: string;
  updated_at?: string;
  completed_at?: string;
  blocked_reason?: string;
  dependencies?: string[];
  workflow_step?: string;
  gate_state?: string;
  gate_reason?: string;
  parent_id?: string;
};

// Slim shape for the child-task list — only what the row renders.
type ChildTask = {
  id?: string;
  title?: string;
  status?: string;
  priority?: number | string;
  parent_id?: string;
};

type HistoryRow = {
  id?: string;
  task_id?: string;
  timestamp?: string;
  from_status?: string;
  to_status?: string;
  reason?: string;
};

type SessionRow = {
  session_id: string;
  started_at?: string | null;
  ended_at?: string | null;
  duration_s?: number;
  tokens_used?: number;
  files_read?: number;
  files_modified?: number;
  skills_invoked?: number;
};

const STATUS_CYCLE: Record<string, string[]> = {
  pending: ["in_progress", "blocked", "done"],
  in_progress: ["done", "blocked", "pending"],
  blocked: ["in_progress", "pending", "done"],
  done: ["pending"],
};

export default function TaskDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  // `from` is the path the back button returns to. A child opened from its
  // parent's detail carries from=/tasks/<parentId>, so back goes up to the
  // parent rather than all the way out to the board.
  const fromState = (location.state as { from?: string } | null)?.from;
  const from = fromState || "/tasks";
  const backLabel = from === "/conductor"
    ? "back to conductor"
    : from.startsWith("/tasks/")
      ? "back to parent"
      : "back to tasks";
  const [project] = useProject();
  const [task, setTask] = useState<Task | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [children, setChildren] = useState<ChildTask[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    try {
      const d = await api.get<{ task: Task; history: HistoryRow[]; sessions?: SessionRow[] }>(
        `/api/tasks/${id}?project=${project}`,
      );
      setTask(d.task);
      setHistory(d.history ?? []);
      setSessions(d.sessions ?? []);
      setError(null);
      // Children aren't on the detail payload — derive them from the task
      // list (parent_id === this id). Cheap, and keeps the API unchanged.
      try {
        const all = await api.get<{ tasks: ChildTask[] }>(`/api/tasks?project=${project}`);
        setChildren((all.tasks ?? []).filter((t) => t.parent_id === id));
      } catch {
        setChildren([]);
      }
    } catch (e) {
      setError((e as Error).message ?? "task not found");
    }
  }, [id, project]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(t);
  }, [notice]);

  const setStatus = async (status: string) => {
    setBusy(true);
    try {
      await fetch(`/api/tasks/${id}?project=${project}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      setNotice(`Moved to ${status}.`);
      load();
    } catch (e) {
      setNotice(`Update failed: ${(e as Error).message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  if (error) {
    return (
      <Page>
        <button
          onClick={() => navigate(from)}
          className="text-[11px] uppercase tracking-wider opacity-60 hover:opacity-100"
        >
          ← {backLabel}
        </button>
        <Card>
          <Empty>{error}</Empty>
        </Card>
      </Page>
    );
  }

  if (!task) {
    return (
      <Page>
        <Card><Empty>Loading…</Empty></Card>
      </Page>
    );
  }

  const transitions = STATUS_CYCLE[task.status ?? "pending"] ?? [];
  const taskStatus = task.status ?? "pending";
  const statusTone = STATUS_TONE[taskStatus] ?? "slate";
  const pTone = priorityTone(task.priority);
  const conductorOn = (task.workflow_step ?? "") !== "" || (task.gate_state ?? "none") !== "none";

  return (
    <Page>
      <button
        onClick={() => navigate(from)}
        className="text-[11px] uppercase tracking-wider opacity-60 hover:opacity-100 self-start"
      >
        ← {backLabel}
      </button>

      {notice && (
        <div className="fixed bottom-6 right-6 z-40 max-w-[420px] rounded-md border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)]/95 backdrop-blur-sm shadow-lg px-4 py-3 text-[12px]">
          {notice}
        </div>
      )}

      <div className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="font-serif text-3xl tracking-tight">{task.title ?? "Untitled task"}</h1>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <span
              className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded"
              style={{
                background: `var(--accent-${statusTone}-bg)`,
                color: `var(--accent-${statusTone}-fg)`,
                boxShadow: `inset 0 0 0 1px var(--accent-${statusTone}-ring)`,
              }}
            >
              {taskStatus}
            </span>
            {typeof task.priority !== "undefined" && (
              <span
                className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded"
                style={{
                  background: `var(--accent-${pTone}-bg)`,
                  color: `var(--accent-${pTone}-fg)`,
                  boxShadow: `inset 0 0 0 1px var(--accent-${pTone}-ring)`,
                }}
              >
                priority {task.priority}
              </span>
            )}
            {task.assigned_agent && (
              <span
                className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded"
                style={{
                  background: "var(--accent-violet-bg)",
                  color: "var(--accent-violet-fg)",
                }}
              >
                {task.assigned_agent}
              </span>
            )}
            {(task.tags ?? []).map((tag) => {
              const tTone = toneFromLabel(tag);
              return (
                <span
                  key={tag}
                  className="text-[10px] font-mono px-1.5 py-0.5 rounded"
                  style={{
                    background: `var(--accent-${tTone}-bg)`,
                    color: `var(--accent-${tTone}-fg)`,
                  }}
                >
                  #{tag}
                </span>
              );
            })}
          </div>
        </div>
        <div className="flex flex-wrap gap-1 shrink-0">
          {transitions.map((target) => (
            <button
              key={target}
              disabled={busy}
              onClick={() => setStatus(target)}
              className="text-[10px] uppercase tracking-wider px-3 py-1.5 rounded bg-[color:var(--midground-base)]/15 hover:bg-[color:var(--midground-base)]/30 disabled:opacity-40"
            >
              → {target}
            </button>
          ))}
        </div>
      </div>

      {task.blocked_reason && (
        <Card>
          <SectionLabel>Blocked because</SectionLabel>
          <div className="text-sm text-rose-300/90 mt-1">{task.blocked_reason}</div>
        </Card>
      )}

      {conductorOn && (
        <Card>
          <SectionLabel>Conductor</SectionLabel>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-x-4 gap-y-3 text-[12px] mt-2 items-start">
            <div>
              <div className="opacity-50 mb-1">step</div>
              {task.workflow_step
                ? <span className={stepChipClass(task.workflow_step)}>{stepLabel(task.workflow_step)}</span>
                : <span className="opacity-50">—</span>}
            </div>
            <div>
              <div className="opacity-50 mb-1">gate</div>
              {task.gate_state && task.gate_state !== "none"
                ? <span className={gateChipClass(task.gate_state as any)}>{gateLabel(task.gate_state as any)}</span>
                : <span className="opacity-50">none</span>}
            </div>
            <div className="md:col-span-1">
              <div className="opacity-50 mb-1">
                {task.gate_state === "passed" ? "validation"
                  : task.gate_state === "failed" ? "failure reason"
                  : "reason"}
              </div>
              {task.gate_reason
                ? <div className="text-[12px] leading-snug opacity-90">{task.gate_reason}</div>
                : <span className="opacity-50">-</span>}
            </div>
          </div>
        </Card>
      )}

      <Card>
        <SectionLabel>Description</SectionLabel>
        {task.description ? (
          <pre className="mt-2 whitespace-pre-wrap text-[13px] leading-relaxed opacity-90 font-sans">
            {task.description}
          </pre>
        ) : (
          <Empty>No description.</Empty>
        )}
      </Card>

      {task.parent_id && (
        <Card>
          <SectionLabel>Parent</SectionLabel>
          <button
            onClick={() => navigate(`/tasks/${task.parent_id}`, { state: { from: "/tasks" } })}
            className="mt-2 text-sm underline decoration-dotted underline-offset-2 hover:opacity-100 font-mono"
          >
            ↑ {String(task.parent_id).slice(0, 8)}
          </button>
        </Card>
      )}

      {children.length > 0 && (
        <Card>
          <SectionLabel>
            Child tasks ({children.filter((c) => (c.status ?? "") === "done").length}/{children.length} done)
          </SectionLabel>
          <div className="space-y-2 mt-2">
            {children.map((c) => {
              const cTone = STATUS_TONE[c.status ?? "pending"] ?? "slate";
              return (
                <button
                  key={c.id}
                  onClick={() => c.id && navigate(`/tasks/${c.id}`, { state: { from: `/tasks/${id}` } })}
                  className="w-full text-left rounded-md border border-[color:var(--midground-base)]/10 bg-[color:var(--background-base)]/30 p-3 hover:border-[color:var(--midground-base)]/40 hover:bg-[color:var(--background-base)]/50 transition-colors flex items-center justify-between gap-3"
                >
                  <span className="text-sm font-medium">{c.title ?? c.id}</span>
                  <span className="flex items-center gap-2 shrink-0">
                    {typeof c.priority !== "undefined" && (
                      <span className="text-[10px] opacity-50 font-mono">p{c.priority}</span>
                    )}
                    <span
                      className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded"
                      style={{
                        background: `var(--accent-${cTone}-bg)`,
                        color: `var(--accent-${cTone}-fg)`,
                        boxShadow: `inset 0 0 0 1px var(--accent-${cTone}-ring)`,
                      }}
                    >
                      {c.status ?? "pending"}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </Card>
      )}

      <Card>
        <SectionLabel>Metadata</SectionLabel>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-2 text-[12px] mt-2">
          <div><span className="opacity-50">id:</span> <span className="font-mono break-all">{task.id}</span></div>
          {task.story_file && <div><span className="opacity-50">story:</span> <span className="font-mono">{task.story_file}</span></div>}
          {task.created_at && <div><span className="opacity-50">created:</span> {String(task.created_at).slice(0, 19)}</div>}
          {task.updated_at && <div><span className="opacity-50">updated:</span> {String(task.updated_at).slice(0, 19)}</div>}
          {task.completed_at && <div><span className="opacity-50">completed:</span> {String(task.completed_at).slice(0, 19)}</div>}
          {(task.dependencies ?? []).length > 0 && (
            <div className="col-span-2 md:col-span-3">
              <span className="opacity-50">dependencies:</span>{" "}
              {(task.dependencies ?? []).map((d, i) => (
                <span key={d} className="font-mono">
                  {i > 0 && ", "}
                  <button
                    onClick={() => navigate(`/tasks/${d}`)}
                    className="underline decoration-dotted underline-offset-2 hover:opacity-100"
                  >
                    {d.slice(0, 8)}
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
      </Card>

      <Card>
        <SectionLabel>Sessions ({sessions.length})</SectionLabel>
        {sessions.length === 0 ? (
          <Empty>No Claude sessions linked to this task yet.</Empty>
        ) : (
          <ul className="divide-y divide-[color:var(--midground-base)]/10 mt-2">
            {sessions.map((s) => (
              <li key={s.session_id} className="py-3">
                <div className="flex items-baseline justify-between gap-3 flex-wrap">
                  <span className="font-mono text-[12px] break-all">{s.session_id}</span>
                  <span className="text-[11px] opacity-50">
                    {s.started_at ? String(s.started_at).slice(0, 19) : "—"}
                    {s.ended_at ? ` → ${String(s.ended_at).slice(0, 19)}` : ""}
                  </span>
                </div>
                <div className="flex flex-wrap gap-2 mt-2">
                  {[
                    ["duration", `${s.duration_s ?? 0}s`],
                    ["tokens", String(s.tokens_used ?? 0)],
                    ["read", String(s.files_read ?? 0)],
                    ["modified", String(s.files_modified ?? 0)],
                    ["skills", String(s.skills_invoked ?? 0)],
                  ].map(([label, value]) => (
                    <span
                      key={label}
                      className="text-[11px] px-2 py-0.5 rounded bg-[color:var(--midground-base)]/10"
                    >
                      <span className="opacity-50">{label}</span>{" "}
                      <span className="font-mono">{value}</span>
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <SectionLabel>History ({history.length})</SectionLabel>
        {history.length === 0 ? (
          <Empty>No transitions recorded.</Empty>
        ) : (
          <ul className="divide-y divide-[color:var(--midground-base)]/10 mt-2">
            {history.map((h, i) => (
              <li key={i} className="py-2 text-[12px] font-mono">
                <span className="opacity-60">{String(h.timestamp ?? "").slice(0, 19)}</span>
                <span className="mx-2 opacity-80">{h.from_status ?? "—"} → {h.to_status ?? "—"}</span>
                {h.reason && <span className="opacity-60">({h.reason})</span>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </Page>
  );
}

