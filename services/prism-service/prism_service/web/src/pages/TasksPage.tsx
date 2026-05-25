import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
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
};

const COLUMNS: { key: string; label: string }[] = [
  { key: "pending", label: "Pending" },
  { key: "in_progress", label: "In Progress" },
  { key: "blocked", label: "Blocked" },
  { key: "done", label: "Done" },
];

export default function TasksPage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [next, setNext] = useState<Task | null>(null);

  const load = useCallback(() => {
    api.get<{ tasks: Task[] }>(`/api/tasks?project=${project}`).then((d) => setTasks(d.tasks)).catch(() => setTasks([]));
    api.get<{ next: Task | null }>(`/api/tasks/next?project=${project}`).then((d) => setNext(d.next)).catch(() => setNext(null));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  return (
    <Page>
      <section className="flex flex-wrap gap-3">
        {COLUMNS.map((c) => (
          <Kpi key={c.key} label={c.label} value={tasks.filter((t) => (t.status ?? "pending") === c.key).length} />
        ))}
      </section>

      <Card>
        <SectionLabel>What's next</SectionLabel>
        {next ? (
          <button
            onClick={() => next.id && navigate(`/tasks/${next.id}`)}
            className="text-sm text-left hover:opacity-100 w-full"
          >
            <div className="font-medium hover:underline decoration-dotted underline-offset-2">{next.title ?? next.id}</div>
            <div className="text-xs opacity-60 mt-1 font-mono">{next.id}</div>
          </button>
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
                  {items.map((t) => (
                    <button
                      key={t.id}
                      onClick={() => t.id && navigate(`/tasks/${t.id}`)}
                      className="w-full text-left rounded-md border border-[color:var(--midground-base)]/10 bg-[color:var(--background-base)]/30 p-3 hover:border-[color:var(--midground-base)]/40 hover:bg-[color:var(--background-base)]/50 transition-colors cursor-pointer"
                    >
                      <div className="text-sm font-medium">{t.title ?? "—"}</div>
                      <div className="flex items-center gap-2 mt-2 flex-wrap">
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
                  ))}
                </div>
              )}
            </Card>
          );
        })}
      </div>
    </Page>
  );
}
