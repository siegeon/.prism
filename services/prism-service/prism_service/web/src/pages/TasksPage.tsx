import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, Kpi, toneFromLabel, type PillTone } from "@/components/ui";
import {
  stepChipClass, gateChipClass, gateLabel, stepLabel,
} from "@/lib/workflowChips";

type Task = {
  id?: string;
  title?: string;
  status?: string;
  priority?: number | string;
  tags?: string[];
  assigned_agent?: string;
  workflow_step?: string;
  gate_state?: string;
  gate_reason?: string;
};

// Workflow tones — same convention TaskDetailPage uses for its status
// chip, so the kanban column header and the per-task pill match.
//   pending      → amber  (waiting for attention)
//   in_progress  → teal   (actively being worked)
//   blocked      → rose   (problem, stop)
//   done         → emerald (complete)
const STATUS_TONE: Record<string, PillTone> = {
  pending: "amber",
  in_progress: "teal",
  blocked: "rose",
  done: "emerald",
};

const COLUMNS: { key: string; label: string }[] = [
  { key: "pending", label: "Pending" },
  { key: "in_progress", label: "In Progress" },
  { key: "blocked", label: "Blocked" },
  { key: "done", label: "Done" },
];

// Priority bands → tone. Lower number = higher priority by convention,
// so p1 reads as urgent rose, p2/3 as warning amber/sage, p4+ neutral
// slate. Strings (e.g. "high"/"medium"/"low") fall through to a hash.
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
            onClick={() => next.id && navigate(`/tasks/${next.id}`, { state: { from: "/tasks" } })}
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
          const colTone = STATUS_TONE[col.key] ?? "slate";
          return (
            <Card key={col.key} className="!p-4">
              <div className="flex items-center justify-between mb-3">
                <span
                  className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded ring-1 ring-inset"
                  style={{
                    background: `var(--accent-${colTone}-bg)`,
                    color: `var(--accent-${colTone}-fg)`,
                    boxShadow: `inset 0 0 0 1px var(--accent-${colTone}-ring)`,
                  }}
                >
                  {col.label}
                </span>
                <span className="text-xs opacity-50">{items.length}</span>
              </div>
              {items.length === 0 ? (
                <div className="text-xs opacity-40 text-center py-6">empty</div>
              ) : (
                <div className="space-y-2">
                  {items.map((t) => {
                    const pTone = priorityTone(t.priority);
                    const step = t.workflow_step ?? "";
                    const gate = t.gate_state ?? "none";
                    const conductorOn = step !== "" || gate !== "none";
                    return (
                      <button
                        key={t.id}
                        onClick={() => t.id && navigate(`/tasks/${t.id}`, { state: { from: "/tasks" } })}
                        className="w-full text-left rounded-md border border-[color:var(--midground-base)]/10 bg-[color:var(--background-base)]/30 p-3 hover:border-[color:var(--midground-base)]/40 hover:bg-[color:var(--background-base)]/50 transition-colors cursor-pointer"
                      >
                        <div className="text-sm font-medium">{t.title ?? "—"}</div>
                        {conductorOn && (
                          <div className="flex items-center gap-1.5 mt-2 flex-wrap">
                            {step && (
                              <span
                                className={stepChipClass(step)}
                                title={`conductor step: ${step}`}
                              >
                                {stepLabel(step)}
                              </span>
                            )}
                            {gate !== "none" && (
                              <span
                                className={gateChipClass(gate as any)}
                                title={t.gate_reason || `gate ${gate}`}
                              >
                                gate {gateLabel(gate as any)}
                              </span>
                            )}
                          </div>
                        )}
                        <div className="flex items-center gap-2 mt-2 flex-wrap">
                          {typeof t.priority !== "undefined" && (
                            <span
                              className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded"
                              style={{
                                background: `var(--accent-${pTone}-bg)`,
                                color: `var(--accent-${pTone}-fg)`,
                                boxShadow: `inset 0 0 0 1px var(--accent-${pTone}-ring)`,
                              }}
                              title={`priority ${t.priority}`}
                            >
                              p{t.priority}
                            </span>
                          )}
                          {t.assigned_agent && (
                            <span className="text-[10px] opacity-60 font-mono">{t.assigned_agent}</span>
                          )}
                          {(t.tags ?? []).slice(0, 3).map((tag) => {
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
                      </button>
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

