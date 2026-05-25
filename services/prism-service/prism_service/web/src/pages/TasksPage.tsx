import { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, Kpi } from "@/components/ui";

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
};

type HistoryRow = {
  id?: string;
  task_id?: string;
  timestamp?: string;
  from_status?: string;
  to_status?: string;
  reason?: string;
};

const COLUMNS: { key: string; label: string }[] = [
  { key: "pending", label: "Pending" },
  { key: "in_progress", label: "In Progress" },
  { key: "blocked", label: "Blocked" },
  { key: "done", label: "Done" },
];

const STATUS_CYCLE: Record<string, string[]> = {
  pending: ["in_progress", "blocked", "done"],
  in_progress: ["done", "blocked", "pending"],
  blocked: ["in_progress", "pending", "done"],
  done: ["pending"],
};

export default function TasksPage() {
  const [project] = useProject();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [next, setNext] = useState<Task | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [history, setHistory] = useState<Record<string, HistoryRow[]>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => {
    api.get<{ tasks: Task[] }>(`/api/tasks?project=${project}`).then((d) => setTasks(d.tasks)).catch(() => setTasks([]));
    api.get<{ next: Task | null }>(`/api/tasks/next?project=${project}`).then((d) => setNext(d.next)).catch(() => setNext(null));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t); }, [load]);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(t);
  }, [notice]);

  const toggleOpen = async (id: string) => {
    const next = open === id ? null : id;
    setOpen(next);
    if (next && !history[next]) {
      try {
        const d = await api.get<{ task: Task; history: HistoryRow[] }>(
          `/api/tasks/${next}?project=${project}`,
        );
        setHistory((h) => ({ ...h, [next]: d.history ?? [] }));
      } catch {
        setHistory((h) => ({ ...h, [next]: [] }));
      }
    }
  };

  const setStatus = async (id: string, status: string) => {
    setBusy(id);
    try {
      await fetch(`/api/tasks/${id}?project=${project}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      setNotice(`Moved to ${status}.`);
      load();
      const d = await api.get<{ task: Task; history: HistoryRow[] }>(
        `/api/tasks/${id}?project=${project}`,
      );
      setHistory((h) => ({ ...h, [id]: d.history ?? [] }));
    } catch (e) {
      setNotice(`Update failed: ${(e as Error).message ?? e}`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Page>
      <section className="flex flex-wrap gap-3">
        {COLUMNS.map((c) => (
          <Kpi key={c.key} label={c.label} value={tasks.filter((t) => (t.status ?? "pending") === c.key).length} />
        ))}
      </section>

      {notice && (
        <div className="fixed bottom-6 right-6 z-40 max-w-[420px] rounded-md border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)]/95 backdrop-blur-sm shadow-lg px-4 py-3 text-[12px]">
          {notice}
        </div>
      )}

      <Card>
        <SectionLabel>What's next</SectionLabel>
        {next ? (
          <div className="text-sm">
            <div className="font-medium">{next.title ?? next.id}</div>
            <div className="text-xs opacity-60 mt-1 font-mono">{next.id}</div>
          </div>
        ) : (
          <Empty>Queue is clear.</Empty>
        )}
      </Card>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        {COLUMNS.map((col) => {
          const items = tasks.filter((t) => (t.status ?? "pending") === col.key);
          return (
            <Card key={col.key} className="!p-4">
              <div className="flex items-center justify-between mb-3">
                <SectionLabel>{col.label}</SectionLabel>
                <span className="text-xs opacity-50 -mt-3">{items.length}</span>
              </div>
              {items.length === 0 ? (
                <div className="text-xs opacity-40 text-center py-6">empty</div>
              ) : (
                <div className="space-y-2">
                  {items.map((t) => {
                    const id = t.id ?? "";
                    const isOpen = open === id;
                    const transitions = STATUS_CYCLE[t.status ?? "pending"] ?? [];
                    return (
                      <div
                        key={id}
                        className="rounded-md border border-[color:var(--midground-base)]/10 bg-[color:var(--background-base)]/30 hover:border-[color:var(--midground-base)]/30 transition-colors"
                      >
                        <button
                          onClick={() => toggleOpen(id)}
                          className="w-full text-left p-3"
                        >
                          <div className="text-sm font-medium flex items-start gap-2">
                            <span className="opacity-50 text-xs mt-0.5">{isOpen ? "▾" : "▸"}</span>
                            <span className="flex-1">{t.title ?? "—"}</span>
                          </div>
                          <div className="flex items-center gap-2 mt-2 ml-5 flex-wrap">
                            {typeof t.priority !== "undefined" && (
                              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--midground-base)]/10 opacity-70">
                                p{t.priority}
                              </span>
                            )}
                            {t.assigned_agent && (
                              <span className="text-[10px] opacity-60 font-mono">{t.assigned_agent}</span>
                            )}
                            {(t.tags ?? []).slice(0, 3).map((tag) => (
                              <span key={tag} className="text-[10px] opacity-50 font-mono">#{tag}</span>
                            ))}
                          </div>
                        </button>
                        {isOpen && (
                          <div className="px-3 pb-3 ml-5 space-y-3">
                            {t.description && (
                              <pre className="whitespace-pre-wrap text-[12px] leading-relaxed opacity-90 font-sans p-2 rounded bg-[color:var(--midground-base)]/5 border border-[color:var(--midground-base)]/10">
                                {t.description}
                              </pre>
                            )}
                            <div className="text-[11px] grid grid-cols-2 gap-x-3 gap-y-1">
                              {id && <div><span className="opacity-50">id:</span> <span className="font-mono">{id.slice(0, 8)}</span></div>}
                              {t.created_at && <div><span className="opacity-50">created:</span> {String(t.created_at).slice(0, 10)}</div>}
                              {t.updated_at && <div><span className="opacity-50">updated:</span> {String(t.updated_at).slice(0, 10)}</div>}
                              {t.completed_at && <div><span className="opacity-50">done:</span> {String(t.completed_at).slice(0, 10)}</div>}
                            </div>
                            {t.blocked_reason && (
                              <div className="text-[11px]">
                                <span className="opacity-50">blocked:</span>{" "}
                                <span className="text-rose-300/90">{t.blocked_reason}</span>
                              </div>
                            )}
                            <div className="flex flex-wrap gap-1 pt-1">
                              {transitions.map((target) => (
                                <button
                                  key={target}
                                  disabled={busy === id}
                                  onClick={() => setStatus(id, target)}
                                  className="text-[10px] uppercase tracking-wider px-2 py-1 rounded bg-[color:var(--midground-base)]/10 hover:bg-[color:var(--midground-base)]/25 disabled:opacity-40"
                                >
                                  → {target}
                                </button>
                              ))}
                            </div>
                            {(history[id] ?? []).length > 0 && (
                              <div className="text-[11px]">
                                <div className="opacity-50 uppercase tracking-wider mb-1">history</div>
                                <ul className="space-y-0.5">
                                  {(history[id] ?? []).slice(-5).map((h, i) => (
                                    <li key={i} className="opacity-80 font-mono">
                                      {String(h.timestamp ?? "").slice(0, 19)} · {h.from_status ?? "—"} → {h.to_status ?? "—"}
                                      {h.reason ? ` (${h.reason})` : ""}
                                    </li>
                                  ))}
                                </ul>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>
          );
        })}
      </div>
    </Page>
  );
}
