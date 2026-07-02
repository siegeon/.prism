import { useEffect, useState, useCallback, useMemo } from "react";
import * as Plot from "@observablehq/plot";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty } from "@/components/ui";
import { cn } from "@/lib/utils";
import { PlotFigure, Sparkline, TONE, plotBase } from "@/components/Chart";

type State = {
  health: {
    flagged_conflicts: number; stuck_tasks: number; stale_brain_docs: number;
    domains_near_cap: string[]; last_governance_run?: string;
  };
  kpis: { brain_docs: number; entities: number; relationships: number; communities: number; memories: number; tasks_active: number };
};

type Recent = { q: string; n_results: number; latency_ms: number; ts: string };
type Activity = {
  days: string[];
  series: { searches: number[]; indexing: number[]; workflow: number[] };
  queries: { per_day: number[]; latency: (number | null)[]; recent: Recent[]; total: number; zero: number; avg_results: number; avg_latency: number | null };
  flow: { created: number[]; completed: number[]; events_by_action: Record<string, number>; gate_passed: number; gate_failed: number; cycle_days: number | null };
  tokens: { per_day: number[]; total: number; sessions: number; avg_session: number; window_total: number };
};

const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
const nf = (n: number) => n.toLocaleString();
/** Compact token counts: 1234 -> 1.2k, 12588741 -> 12.6M. */
const compact = (n: number) =>
  n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n);

/** KPI card whose number is contextualized by a trend sparkline + today's
 *  delta — the dashboard shows movement, not just a static count. */
function TrendKpi({ label, series, color, fmt }: { label: string; series: number[]; color: string; fmt?: (n: number) => string }) {
  const total = sum(series);
  const today = series[series.length - 1] ?? 0;
  const prev = series[series.length - 2] ?? 0;
  // Day-over-day delta: the arrow sign AND the number both derive from
  // (today - prev), so a KPI whose total rose today never shows the bare
  // absolute count behind a red down-arrow (task 855ef8aa). Previously the
  // arrow encoded rate direction while the number was today's absolute
  // count — "▼ 9 today" read as a loss even though 9 were just added.
  const delta = today - prev;
  const up = delta >= 0;
  const signed = `${up ? "+" : "−"}${(fmt ?? nf)(Math.abs(delta))}`;
  return (
    <div className="flex-1 min-w-[170px] rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] p-4">
      <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-label)] mb-2">{label}</div>
      <div className="flex items-end justify-between gap-2">
        <div className="text-2xl font-semibold leading-none text-[color:var(--text-primary)]">{(fmt ?? nf)(total)}</div>
        <Sparkline data={series} color={color} />
      </div>
      <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] mt-2">
        <span style={{ color }}>{up ? "▲" : "▼"} {signed}</span> vs prev day
      </div>
    </div>
  );
}

/** Inline labelled stat for the panel stat-strips. */
function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex-1 min-w-[88px]">
      <div className="text-lg font-semibold leading-none" style={{ color: tone ?? "var(--text-primary)" }}>{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] mt-1">{label}</div>
    </div>
  );
}

function Row({ label, v, bad }: { label: string; v: number; bad: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <span className="opacity-80">{label}</span>
      <span className={cn("font-mono text-xs px-2 py-0.5 rounded",
        bad ? "bg-amber-400/15 text-amber-200" : "bg-emerald-400/10 text-emerald-200/80",
      )}>{v}</span>
    </div>
  );
}

type Drift = { understand?: boolean; graph?: boolean; brain?: boolean };

// Drift / staleness card: shows which source-derived indexes have fallen
// behind the project's pinned SHA, with a re-sync action wired to
// POST /api/staleness/resync (rebuilds the graph + advances last_analyzed_sha).
function StalenessCard({ project }: { project: string }) {
  const [drift, setDrift] = useState<Drift>({});
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .get<Drift>(`/api/staleness?project=${encodeURIComponent(project)}`)
      .then(setDrift)
      .catch(() => setDrift({}));
  }, [project]);

  useEffect(() => { load(); }, [load]);

  const resync = async () => {
    setBusy(true);
    setNote(null);
    try {
      const r = await fetch(`/api/staleness/resync?project=${encodeURIComponent(project)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const b = await r.json().catch(() => ({}));
      setNote(r.ok ? "Re-synced." : `Re-sync failed: ${b.detail ?? r.statusText}`);
      load();
    } catch (e) {
      setNote(`Re-sync failed: ${(e as Error).message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  const anyStale = Boolean(drift.understand || drift.graph || drift.brain);
  return (
    <Card>
      <div className="flex items-center justify-between gap-3">
        <SectionLabel>Drift / staleness</SectionLabel>
        <button
          type="button"
          disabled={busy}
          onClick={resync}
          className="text-[10px] uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
          style={{ background: "var(--accent-teal-bg)", color: "var(--accent-teal-fg)" }}
        >
          {busy ? "Re-syncing…" : "Re-sync"}
        </button>
      </div>
      <div className="space-y-1.5 mt-2 text-sm">
        <Row label="understand" v={drift.understand ? 1 : 0} bad={Boolean(drift.understand)} />
        <Row label="graph" v={drift.graph ? 1 : 0} bad={Boolean(drift.graph)} />
        <Row label="brain" v={drift.brain ? 1 : 0} bad={Boolean(drift.brain)} />
      </div>
      <div className="text-[11px] opacity-60 mt-2">
        {note ?? (anyStale ? "Some indexes are behind the pinned SHA — re-sync to rebuild." : "All indexes current.")}
      </div>
    </Card>
  );
}

export default function DashboardPage() {
  const [project] = useProject();
  const [data, setData] = useState<State | null>(null);
  const [act, setAct] = useState<Activity | null>(null);

  const load = useCallback(() => {
    api.get<State>(`/api/dashboard/state?project=${project}`).then(setData).catch(() => setData(null));
    api.get<Activity>(`/api/dashboard/activity?project=${project}&days=14`).then(setAct).catch(() => setAct(null));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  // Hero: overlaid multi-series timeline on a symlog axis so the giant
  // reindex spikes and single-digit search counts are both legible.
  const pulse = useMemo(() => {
    if (!act) return null;
    const defs = [["indexing", act.series.indexing, TONE.sage], ["workflow", act.series.workflow, TONE.violet], ["searches", act.series.searches, TONE.teal]] as const;
    const rows = defs.flatMap(([kind, arr]) => act.days.map((d, i) => ({ date: new Date(d), kind, value: arr[i] })));
    return {
      ...plotBase, height: 210, marginLeft: 50, marginRight: 16, marginTop: 16, marginBottom: 26, width: 880,
      x: { type: "utc", label: null, ticks: 7, tickFormat: "%b %-d" },
      y: { type: "symlog", label: "events / day (log)", grid: true, domain: [0, 10000], ticks: [0, 10, 100, 1000, 10000], tickFormat: (d: number) => (d >= 1000 ? `${d / 1000}k` : `${d}`) },
      color: { domain: defs.map((d) => d[0]), range: defs.map((d) => d[2]), legend: true },
      marks: [
        Plot.ruleY([0], { stroke: "#8b97ad", strokeOpacity: 0.2 }),
        Plot.lineY(rows, { x: "date", y: "value", stroke: "kind", curve: "monotone-x", strokeWidth: 1.75 }),
        Plot.dot(rows.filter((r) => r.value > 0), { x: "date", y: "value", fill: "kind", r: 2.2 }),
      ],
    } as Plot.PlotOptions;
  }, [act]);

  const qChart = useMemo(() => {
    if (!act) return null;
    const rows = act.days.map((d, i) => ({ date: new Date(d), q: act.queries.per_day[i] }));
    const max = Math.max(1, ...act.queries.per_day);
    return {
      ...plotBase, height: 130, marginLeft: 28, marginRight: 8, marginTop: 8, marginBottom: 22, width: 420,
      x: { type: "utc", label: null, ticks: 5, tickFormat: "%b %-d" },
      y: { label: null, grid: true, ticks: Math.min(3, max), domain: [0, max], tickFormat: "d" },
      marks: [Plot.rectY(rows, { x: "date", y: "q", interval: "day", fill: TONE.teal, fillOpacity: 0.85, rx: 1 }), Plot.ruleY([0], { stroke: "#8b97ad", strokeOpacity: 0.2 })],
    } as Plot.PlotOptions;
  }, [act]);

  const flowChart = useMemo(() => {
    if (!act) return null;
    const mk = (arr: number[], kind: string) => act.days.map((d, i) => ({ date: new Date(d), kind, value: arr[i] }));
    const rows = [...mk(act.flow.created, "created"), ...mk(act.flow.completed, "completed")];
    const max = Math.max(1, ...act.flow.created, ...act.flow.completed);
    return {
      ...plotBase, height: 130, marginLeft: 28, marginRight: 8, marginTop: 8, marginBottom: 22, width: 420,
      x: { type: "utc", label: null, ticks: 5, tickFormat: "%b %-d" },
      y: { label: null, grid: true, ticks: Math.min(3, max), domain: [0, max], tickFormat: "d" },
      color: { domain: ["created", "completed"], range: [TONE.amber, TONE.emerald], legend: true },
      marks: [Plot.ruleY([0], { stroke: "#8b97ad", strokeOpacity: 0.2 }), Plot.lineY(rows, { x: "date", y: "value", stroke: "kind", curve: "monotone-x", strokeWidth: 1.75 }), Plot.dot(rows.filter((r) => r.value > 0), { x: "date", y: "value", fill: "kind", r: 2 })],
    } as Plot.PlotOptions;
  }, [act]);

  const eventsChart = useMemo(() => {
    if (!act) return null;
    const rows = Object.entries(act.flow.events_by_action).map(([action, count]) => ({ action, count }));
    if (!rows.length) return null;
    return {
      ...plotBase, height: 26 * rows.length + 26, marginLeft: 92, marginRight: 28, marginTop: 4, marginBottom: 20, width: 420,
      x: { label: null, grid: true, ticks: 4 },
      y: { label: null },
      marks: [Plot.barX(rows, { x: "count", y: "action", fill: TONE.violet, fillOpacity: 0.8, rx: 1, sort: { y: "x", reverse: true } }), Plot.text(rows, { x: "count", y: "action", text: (d) => nf(d.count), dx: 12, fill: "#8b97ad", fontSize: 10 })],
    } as Plot.PlotOptions;
  }, [act]);

  const tokenChart = useMemo(() => {
    if (!act) return null;
    const rows = act.days.map((d, i) => ({ date: new Date(d), tokens: act.tokens.per_day[i] }));
    return {
      ...plotBase, height: 150, marginLeft: 40, marginRight: 8, marginTop: 8, marginBottom: 22, width: 420,
      x: { type: "utc", label: null, ticks: 5, tickFormat: "%b %-d" },
      y: { label: null, grid: true, ticks: 3, tickFormat: (n: number) => compact(n) },
      marks: [Plot.areaY(rows, { x: "date", y: "tokens", fill: TONE.amber, fillOpacity: 0.18, curve: "monotone-x" }), Plot.lineY(rows, { x: "date", y: "tokens", stroke: TONE.amber, strokeWidth: 1.75, curve: "monotone-x" }), Plot.ruleY([0], { stroke: "#8b97ad", strokeOpacity: 0.2 })],
    } as Plot.PlotOptions;
  }, [act]);

  const health = data?.health;
  const q = act?.queries;
  const zeroPct = q && q.total ? Math.round((q.zero / q.total) * 100) : 0;
  const gates = act ? act.flow.gate_passed + act.flow.gate_failed : 0;
  const gatePct = gates ? Math.round((act!.flow.gate_passed / gates) * 100) : null;

  return (
    <Page>
      {/* Hero — the brain's pulse over time */}
      <Card raised>
        <SectionLabel>Brain activity · last 14 days</SectionLabel>
        {pulse ? <PlotFigure options={pulse} className="w-full" /> : <Empty>No activity yet.</Empty>}
      </Card>

      <StalenessCard project={project} />

      {/* Trend KPIs — movement, not static inventory */}
      <section className="flex flex-wrap gap-3">
        <TrendKpi label="Queries" series={act?.series.searches ?? []} color={TONE.teal} />
        <TrendKpi label="Docs indexed" series={act?.series.indexing ?? []} color={TONE.sage} />
        <TrendKpi label="Workflow events" series={act?.series.workflow ?? []} color={TONE.violet} />
        <TrendKpi label="Tasks shipped" series={act?.flow.completed ?? []} color={TONE.emerald} />
        <TrendKpi label="Tokens (14d)" series={act?.tokens.per_day ?? []} color={TONE.amber} fmt={compact} />
      </section>

      {/* Interactions + Delivery flow */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <SectionLabel>Interactions · search</SectionLabel>
          <div className="flex gap-3 mb-3">
            <Stat label="queries" value={nf(q?.total ?? 0)} />
            <Stat label="avg latency" value={q?.avg_latency != null ? `${q.avg_latency} ms` : "—"} />
            <Stat label="avg results" value={q ? String(q.avg_results) : "—"} tone={q && q.avg_results < 1 ? TONE.rose : undefined} />
            <Stat label="zero-result" value={`${zeroPct}%`} tone={zeroPct > 50 ? TONE.rose : undefined} />
          </div>
          {qChart && <PlotFigure options={qChart} className="w-full mb-3" />}
          <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-label)] mb-2">Recent queries</div>
          {q?.recent.length ? (
            <ul className="space-y-1.5">
              {q.recent.map((r, i) => (
                <li key={i} className="flex items-center gap-2 text-sm">
                  <span className="font-mono text-[10px] px-1.5 py-0.5 rounded shrink-0"
                    style={{ background: r.n_results ? "rgba(110,231,183,0.12)" : "rgba(249,168,212,0.14)", color: r.n_results ? TONE.emerald : TONE.rose }}>{r.n_results}</span>
                  <span className="truncate text-[color:var(--text-secondary)]">{r.q}</span>
                  <span className="ml-auto shrink-0 text-[10px] text-[color:var(--text-muted)]">{r.latency_ms} ms</span>
                </li>
              ))}
            </ul>
          ) : <Empty>No searches yet.</Empty>}
        </Card>

        <Card>
          <SectionLabel>Delivery flow</SectionLabel>
          <div className="flex gap-3 mb-3">
            <Stat label="gate pass-rate" value={gatePct != null ? `${gatePct}%` : "—"} tone={gatePct === 100 ? TONE.emerald : gatePct != null ? TONE.amber : undefined} />
            <Stat label="cycle time" value={act?.flow.cycle_days != null ? `${act.flow.cycle_days} d` : "—"} />
            <Stat label="shipped" value={nf(sum(act?.flow.completed ?? []))} />
            <Stat label="active" value={nf(data?.kpis.tasks_active ?? 0)} />
          </div>
          {flowChart && <PlotFigure options={flowChart} className="w-full mb-3" />}
          <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-label)] mb-1">Workflow events by action</div>
          {eventsChart ? <PlotFigure options={eventsChart} className="w-full" /> : <Empty>No events.</Empty>}
        </Card>
      </div>

      {/* Token usage + Governance */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-2">
          <SectionLabel>Token usage · per work session</SectionLabel>
          <div className="flex gap-3 mb-3">
            <Stat label="total tokens" value={compact(act?.tokens.total ?? 0)} tone={TONE.amber} />
            <Stat label="avg / session" value={compact(act?.tokens.avg_session ?? 0)} />
            <Stat label="sessions" value={nf(act?.tokens.sessions ?? 0)} />
            <Stat label="last 14 days" value={compact(act?.tokens.window_total ?? 0)} />
          </div>
          {tokenChart ? <PlotFigure options={tokenChart} className="w-full" /> : <Empty>No token data yet.</Empty>}
          <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] mt-2">Tracked per session — PRISM does not record tokens per individual task.</div>
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
                <div className="text-[10px] uppercase tracking-wider opacity-50 pt-2">Last run: {health.last_governance_run}</div>
              )}
            </div>
          ) : <Empty>—</Empty>}
        </Card>
      </div>
    </Page>
  );
}
