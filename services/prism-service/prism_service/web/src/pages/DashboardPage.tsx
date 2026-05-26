import { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, Kpi, SectionLabel, Empty } from "@/components/ui";
import WorkflowLanes, { type Step, type Workflow } from "@/components/WorkflowLanes";

type State = {
  workflow: Workflow;
  steps: Step[];
  health: {
    flagged_conflicts: number; stuck_tasks: number; stale_brain_docs: number;
    domains_near_cap: string[]; last_governance_run?: string;
  };
  kpis: {
    brain_docs: number; entities: number; relationships: number;
    communities: number; memories: number; tasks_active: number;
  };
};

export default function DashboardPage() {
  const [project] = useProject();
  const [data, setData] = useState<State | null>(null);

  const load = useCallback(() => {
    api.get<State>(`/api/dashboard/state?project=${project}`).then(setData).catch(() => setData(null));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t); }, [load]);

  const kpis = data?.kpis;
  const health = data?.health;
  const steps = data?.steps ?? [];
  const workflow = data?.workflow;

  return (
    <Page>
      <section className="flex flex-wrap gap-3">
        <Kpi label="Brain docs" value={kpis?.brain_docs ?? "—"} />
        <Kpi label="Entities" value={kpis?.entities ?? "—"} />
        <Kpi label="Relationships" value={kpis?.relationships ?? "—"} />
        <Kpi label="Communities" value={kpis?.communities ?? "—"} />
        <Kpi label="Memories" value={kpis?.memories ?? "—"} />
        <Kpi label="Active tasks" value={kpis?.tasks_active ?? "—"} />
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-2">
          <div className="flex items-center justify-between mb-3">
            <SectionLabel>Workflow</SectionLabel>
            <span
              className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded"
              style={workflow?.active ? {
                background: "var(--accent-emerald-bg)",
                color: "var(--accent-emerald-fg)",
                boxShadow: "inset 0 0 0 1px var(--accent-emerald-ring)",
              } : {
                background: "var(--accent-slate-bg)",
                color: "var(--accent-slate-fg)",
              }}
            >{workflow?.active ? "running" : "idle"}</span>
          </div>
          {steps.length === 0 ? (
            <Empty>No workflow steps defined.</Empty>
          ) : (
            <WorkflowLanes steps={steps} workflow={workflow} />
          )}
        </Card>

        <Card>
          <SectionLabel>Governance</SectionLabel>
          {health ? (
            <div className="space-y-2 text-sm">
              <Row label="Flagged conflicts" v={health.flagged_conflicts} bad={health.flagged_conflicts > 0} />
              <Row label="Stuck tasks" v={health.stuck_tasks} bad={health.stuck_tasks > 0} />
              <Row label="Stale brain docs" v={health.stale_brain_docs} bad={health.stale_brain_docs > 0} />
              <Row label="Domains near cap" v={health.domains_near_cap.length} bad={health.domains_near_cap.length > 0} />
              {health.last_governance_run && (
                <div className="text-[10px] uppercase tracking-wider opacity-50 pt-2">
                  Last run: {health.last_governance_run}
                </div>
              )}
            </div>
          ) : <Empty>—</Empty>}
        </Card>
      </div>
    </Page>
  );
}

function Row({ label, v, bad }: { label: string; v: number; bad: boolean }) {
  const tone = bad ? "amber" : "emerald";
  return (
    <div className="flex items-center justify-between">
      <span className="opacity-80">{label}</span>
      <span
        className="font-mono text-xs px-2 py-0.5 rounded"
        style={{
          background: `var(--accent-${tone}-bg)`,
          color: `var(--accent-${tone}-fg)`,
        }}
      >{v}</span>
    </div>
  );
}
