import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, toneFromLabel } from "@/components/ui";
import Markdown from "@/components/Markdown";

type Task = {
  id?: string;
  title?: string;
  description?: string;
  status?: string;
  tags?: string[];
  completed_at?: string;
  parent_id?: string;
};

// Completed tasks never sit on the active board (TasksPage dropped the Done
// column — the done-tasks-off-board doctrine). They land here instead: ones
// the reflection worker absorbed link into Memory, the rest are archived.
// This is the "remove where it doesn't make sense, absorb where it does"
// surface, re-landed from stranded commit bdb8bad.
export default function CompletedTasksPage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [memCounts, setMemCounts] = useState<Record<string, number>>({});

  // memory_counts is optional in the payload (the backend half of 2caae5d is
  // not on this branch); absent -> {} and every card reads "archived".
  const load = useCallback(() => {
    api.get<{ tasks: Task[]; memory_counts?: Record<string, number> }>(`/api/tasks?project=${project}`)
      .then((d) => { setTasks(d.tasks); setMemCounts(d.memory_counts ?? {}); })
      .catch(() => { setTasks([]); setMemCounts({}); });
  }, [project]);

  useEffect(() => { load(); }, [load]);

  const done = tasks
    .filter((t) => !t.parent_id && (t.status ?? "") === "done")
    .sort((a, b) => (b.completed_at ?? "").localeCompare(a.completed_at ?? ""));
  const absorbed = done.filter((t) => (memCounts[t.id ?? ""] ?? 0) > 0).length;

  return (
    <Page>
      <button
        onClick={() => navigate("/tasks")}
        className="text-xs uppercase tracking-wider opacity-60 hover:opacity-100 transition-opacity w-fit"
      >
        ← Back to board
      </button>

      <Card>
        <SectionLabel>Completed — {done.length} task{done.length === 1 ? "" : "s"} ({absorbed} in memory)</SectionLabel>
        <p className="text-xs opacity-60 mt-1">
          Finished work lives here, off the active board. Tasks the learning loop
          absorbed are linked into Memory; the rest are archived.
        </p>
      </Card>

      {done.length === 0 ? (
        <Empty>No completed tasks yet.</Empty>
      ) : (
        <div className="space-y-2">
          {done.map((t) => {
            const n = memCounts[t.id ?? ""] ?? 0;
            return (
              <button
                key={t.id}
                onClick={() => t.id && navigate(`/tasks/${t.id}`, { state: { from: "/tasks/completed" } })}
                className="w-full text-left rounded-md border border-[color:var(--midground-base)]/10 bg-[color:var(--background-base)]/30 p-3 hover:border-[color:var(--midground-base)]/40 hover:bg-[color:var(--background-base)]/50 transition-colors"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="text-sm font-medium">{t.title ?? "—"}</div>
                  {n > 0 ? (
                    <span
                      title={`${n} memory(ies) minted from this task — absorbed into Memory`}
                      onClick={(e) => { e.stopPropagation(); navigate("/memory"); }}
                      className="shrink-0 text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ring-1 ring-inset cursor-pointer"
                      style={{
                        background: "var(--accent-emerald-bg)",
                        color: "var(--accent-emerald-fg)",
                        boxShadow: "inset 0 0 0 1px var(--accent-emerald-ring)",
                      }}
                    >
                      in memory ✓
                    </span>
                  ) : (
                    <span className="shrink-0 text-[10px] uppercase tracking-wider opacity-40">archived</span>
                  )}
                </div>
                {t.description && (
                  // Same structured-description preview the active board cards
                  // ship (Markdown rework v6.5.3) — clamped, full doc on the
                  // detail page. The preview survives on the completed screen.
                  <div className="mt-1.5 max-h-28 overflow-hidden [mask-image:linear-gradient(to_bottom,black_70%,transparent)]">
                    <Markdown text={t.description} className="space-y-1.5 text-left" />
                  </div>
                )}
                {(t.tags ?? []).length > 0 && (
                  <div className="flex items-center gap-2 mt-2 flex-wrap">
                    {(t.tags ?? []).slice(0, 4).map((tag) => {
                      const tone = toneFromLabel(tag);
                      return (
                        <span key={tag} className="text-[10px] font-mono px-1.5 py-0.5 rounded"
                          style={{ background: `var(--accent-${tone}-bg)`, color: `var(--accent-${tone}-fg)` }}>
                          #{tag}
                        </span>
                      );
                    })}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      )}
    </Page>
  );
}
