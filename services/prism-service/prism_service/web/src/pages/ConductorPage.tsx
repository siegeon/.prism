import { useEffect, useState, useCallback, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, Kpi, SectionLabel, Empty } from "@/components/ui";
import {
  stepLabel, gateLabel, WORKFLOW_STEPS_ORDERED,
} from "@/lib/workflowChips";

type ManagedTask = {
  id: string;
  title: string;
  workflow_step?: string;
  gate_state?: string;
  gate_reason?: string;
  status?: string;
};

type State = {
  managed_tasks?: ManagedTask[];
  step_buckets?: Record<string, number>;
};

const PERSONA_TONE: Record<string, string> = {
  sm: "teal",
  dev: "amber",
  qa: "violet",
};

const GATE_TONE: Record<string, string> = {
  pending: "amber",
  passed: "emerald",
  failed: "rose",
};

export default function ConductorPage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const [data, setData] = useState<State | null>(null);

  const load = useCallback(() => {
    api.get<State>(`/api/conductor/state?project=${project}`).then(setData).catch(() => setData(null));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  const managed = data?.managed_tasks ?? [];

  const pendingGates = useMemo(() => managed.filter(t => t.gate_state === "pending").length, [managed]);
  const failedGates = useMemo(() => managed.filter(t => t.gate_state === "failed").length, [managed]);
  const modalStep = useMemo(() => {
    const buckets: Record<string, number> = {};
    for (const t of managed) {
      const s = t.workflow_step ?? "";
      if (s) buckets[s] = (buckets[s] ?? 0) + 1;
    }
    const entries = Object.entries(buckets);
    if (entries.length === 0) return "—";
    entries.sort((a, b) => b[1] - a[1]);
    return stepLabel(entries[0][0]);
  }, [managed]);

  // Group tasks by their workflow_step; preserves order matching the lane.
  const tasksByStep = useMemo(() => {
    const m: Record<string, ManagedTask[]> = {};
    for (const t of managed) {
      const s = t.workflow_step ?? "";
      if (!s) continue;
      (m[s] ??= []).push(t);
    }
    return m;
  }, [managed]);

  return (
    <Page>
      <section className="flex flex-wrap gap-3">
        <Kpi label="Under management" value={managed.length} />
        <Kpi label="Pending gates" value={pendingGates} />
        <Kpi label="Failed gates" value={failedGates} />
        <Kpi label="Modal step" value={modalStep} />
      </section>

      <Card>
        <SectionLabel>SDLC swimlanes</SectionLabel>
        <p className="text-[11px] opacity-60 mt-1 mb-3">
          Conductor forces opt-in tasks through these 8 steps top-to-bottom. Tasks worked
          without conductor (status flips only) don't appear here. Click a task pill to open it.
        </p>
        <div className="divide-y divide-[color:var(--midground-base)]/10">
          {WORKFLOW_STEPS_ORDERED.map((s) => {
            const isGate = s.type === "gate";
            const laneTone = isGate ? "slate" : (PERSONA_TONE[s.persona] ?? "slate");
            const tasksHere = tasksByStep[s.id] ?? [];
            return (
              <div
                key={s.id}
                className="grid grid-cols-[14rem_1fr] gap-4 items-center py-3"
              >
                <div className="flex items-center gap-2">
                  <span
                    className="text-[10px] uppercase tracking-wider font-mono px-2 py-1 rounded ring-1"
                    style={{
                      background: `var(--accent-${laneTone}-bg)`,
                      color: `var(--accent-${laneTone}-fg)`,
                      boxShadow: `inset 0 0 0 1px var(--accent-${laneTone}-ring)`,
                    }}
                  >
                    {isGate ? `🚪 ${stepLabel(s.id)}` : stepLabel(s.id)}
                  </span>
                  {!isGate && (
                    <span className="text-[10px] uppercase opacity-50 font-mono">{s.persona}</span>
                  )}
                </div>
                <div className="flex flex-wrap gap-2 min-h-[1.5rem] items-center">
                  {tasksHere.length === 0 ? (
                    <span className="text-[11px] opacity-30 italic">empty</span>
                  ) : (
                    tasksHere.map((t) => {
                      // Gate-row pills carry the gate_state color; agent-row pills
                      // use the lane tone so all tasks at a step read together.
                      const gate = t.gate_state ?? "none";
                      const pillTone = isGate && gate !== "none"
                        ? (GATE_TONE[gate] ?? "slate")
                        : laneTone;
                      return (
                        <button
                          key={t.id}
                          onClick={() => navigate(`/tasks/${t.id}`, { state: { from: "/conductor" } })}
                          className="text-[12px] px-2.5 py-1 rounded ring-1 max-w-[24rem] truncate text-left hover:opacity-100"
                          style={{
                            background: `var(--accent-${pillTone}-bg)`,
                            color: `var(--accent-${pillTone}-fg)`,
                            boxShadow: `inset 0 0 0 1px var(--accent-${pillTone}-ring)`,
                          }}
                          title={`${t.title}\nid: ${t.id}${gate !== "none" ? `\ngate: ${gateLabel(gate as any)}${t.gate_reason ? "\n" + t.gate_reason : ""}` : ""}`}
                        >
                          <span>{t.title}</span>
                          {isGate && gate !== "none" && (
                            <span className="ml-2 opacity-80 text-[10px] uppercase font-mono">
                              {gateLabel(gate as any)}
                            </span>
                          )}
                        </button>
                      );
                    })
                  )}
                </div>
              </div>
            );
          })}
        </div>
        {managed.length === 0 && (
          <Empty>No tasks under conductor management. Call conductor_advance on a task to start one.</Empty>
        )}
      </Card>
    </Page>
  );
}

