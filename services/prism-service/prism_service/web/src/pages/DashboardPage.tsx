import { useEffect, useState, useCallback, useMemo } from "react";
import * as Plot from "@observablehq/plot";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, Skeleton } from "@/components/ui";
import { Lozenge } from "@/components/Lozenge";
import { PlotFigure, Sparkline, TONE, plotBase } from "@/components/Chart";
import { fmtTokens } from "@/lib/format";
import { motion } from "motion/react";
import ConnectExistingPrism from "@/components/ConnectExistingPrism";

type State = {
  health: {
    flagged_conflicts: number; stuck_tasks: number; stale_brain_docs: number;
    domains_near_cap: string[]; last_governance_run?: string;
  };
  kpis: { brain_docs: number; entities: number; relationships: number; communities: number; memories: number; tasks_active: number };
};

type StrandedRow = {
  task_id: string; title: string; commits_ahead: number;
  branch_on_origin: boolean; state: "local_only" | "pushed_unmerged";
};

type Recent = { q: string; n_results: number; latency_ms: number; ts: string };
type Activity = {
  days: string[];
  series: { searches: number[]; indexing: number[]; workflow: number[] };
  queries: {
    recent_zero?: number;
    recent_total?: number;
    recent_rate?: number | null;
    recent_days?: number; per_day: number[]; latency: (number | null)[]; recent: Recent[]; total: number; zero: number; avg_results: number; avg_latency: number | null };
  flow: { created: number[]; completed: number[]; events_by_action: Record<string, number>; gate_passed: number; gate_failed: number; cycle_days: number | null };
  tokens: { per_day: number[]; total: number; sessions: number; avg_session: number; window_total: number };
};

const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
const nf = (n: number) => n.toLocaleString();
/** Compact token counts: 1234 -> 1.2k, 12588741 -> 12.6M (shared k/M/B). */
const compact = (n: number) => fmtTokens(n);

/** KPI card whose number is contextualized by a trend sparkline + today's
 *  delta — the dashboard shows movement, not just a static count. */
function TrendKpi({ label, series, color, fmt }: { label: string; series: number[]; color: string; fmt?: (n: number) => string }) {
  const total = sum(series);
  const today = series[series.length - 1] ?? 0;
  const prev = series[series.length - 2] ?? 0;
  const up = today >= prev;
  return (
    <div className="flex-1 min-w-[170px] rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] p-4">
      <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)] mb-2">{label}</div>
      <div className="flex items-end justify-between gap-2">
        <div className="text-2xl font-semibold leading-none text-[color:var(--text-primary)]">{(fmt ?? nf)(total)}</div>
        <Sparkline data={series} color={color} />
      </div>
      <div className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] mt-2">
        <span style={{ color }}>{up ? "▲" : "▼"} {nf(today)}</span> today
      </div>
    </div>
  );
}

/** Inline labelled stat for the panel stat-strips. */
function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex-1 min-w-[88px]">
      <div className="text-lg font-semibold leading-none" style={{ color: tone ?? "var(--text-primary)" }}>{value}</div>
      <div className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] mt-1">{label}</div>
    </div>
  );
}

function Row({ label, v, bad }: { label: string; v: number; bad: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <span className="opacity-80">{label}</span>
      <Lozenge tone={bad ? "warn" : "ok"} className="tabular-nums">{v}</Lozenge>
    </div>
  );
}

type CoverageSample = { entries: number; indexed: number; ratio: number; measured_at: string };

type Coverage = CoverageSample & { history?: CoverageSample[] };

type Drift = { understand?: boolean; graph?: boolean; brain?: boolean };

// Drift / staleness card: shows which source-derived indexes have fallen
// behind the project's pinned SHA, with a re-sync action wired to
// POST /api/staleness/resync (rebuilds the graph + advances last_analyzed_sha).
function StalenessCard({ project, nothingIndexed = false }: { project: string; nothingIndexed?: boolean }) {
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
          className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
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
      <div className="text-2xs opacity-60 mt-2">
        {note ?? (nothingIndexed
          ? "Nothing is indexed on this install yet, so there is nothing to be behind."
          : anyStale ? "Some indexes are behind the pinned SHA — re-sync to rebuild." : "All indexes current.")}
      </div>
    </Card>
  );
}

/**
 * The honest zero-state (task b064db4e, AC-4).
 *
 * A hydrated install with nothing in it used to render 0/0/0 and "No activity
 * yet.", which is indistinguishable from "your work vanished". That ambiguity
 * cost the owner an hour on a second machine. When we KNOW the instance is
 * genuinely empty, say which of the two it is, and offer the way out.
 */
function FreshInstallPanel() {
  return (
    <div className="rounded-md border border-dashed border-[color:var(--border-default)] px-5 py-6">
      <div className="text-[15px] font-bold mb-2">This PRISM is new and empty.</div>
      <p className="text-[14px] leading-relaxed text-[color:var(--text-secondary)] max-w-[52ch]">
        Nothing is missing. This install has never indexed a project or recorded a task, so there is no history to show yet. If your work lives on another machine, reach that PRISM instead of building a second one here.
      </p>
      <ConnectExistingPrism compact />
    </div>
  );
}

// Knowledge coverage over time (task 0ee4dc98). A number on its own was not
// enough: this decay hid for weeks because a figure existed that nobody
// watched. Every finished play appends a sample, and the chart animates on
// mount and again whenever the series gains one.
//
// Every sample renders through ONE code path. There is no styling keyed on
// which way the value moved, and the series is never filtered by value --
// a dip has to read exactly as clearly as a climb, because the dip is the
// event a person needs to act on.
function CoverageTrend({ series }: { series: CoverageSample[] }) {
  const w = 260;
  const h = 56;
  const pad = 4;
  const pts = series.map((s, i) => {
    const x = series.length > 1
      ? pad + (i * (w - pad * 2)) / (series.length - 1)
      : w / 2;
    const y = h - pad - Math.max(0, Math.min(1, s.ratio)) * (h - pad * 2);
    return { x, y, s };
  });
  const path = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");

  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} role="img"
         aria-label="knowledge coverage over time">
      <motion.polyline
        points={path}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        className="text-[color:var(--accent)]"
        initial={{ pathLength: 0, opacity: 0 }}
        animate={{ pathLength: 1, opacity: 1 }}
        transition={{ duration: 0.7, ease: "easeOut" }}
      />
      {pts.map((p, i) => (
        <motion.circle
          // Keyed by stable index, not by timestamp: when the series gains a
          // sample, React must REUSE the existing nodes and animate them to
          // their new positions. Keying by measured_at remounts every element
          // on every render, so the chart would blink instead of moving.
          key={`pt-${i}`}
          r={2.5}
          fill="currentColor"
          className="text-[color:var(--accent)]"
          // The POSITION is animated, not just the opacity. When a sample
          // lands the series re-scales, and every existing point glides to
          // its new place instead of snapping -- that movement is what makes
          // a change legible without a page reload.
          initial={{ cx: p.x, cy: h - pad, opacity: 0 }}
          animate={{ cx: p.x, cy: p.y, opacity: 1 }}
          transition={{ duration: 0.45, ease: "easeOut" }}
        >
          <title>{`${p.s.indexed} / ${p.s.entries} at ${p.s.measured_at}`}</title>
        </motion.circle>
      ))}
    </svg>
  );
}


export default function DashboardPage() {
  const [project] = useProject();
  const [data, setData] = useState<State | null>(null);
  const [act, setAct] = useState<Activity | null>(null);
  // Hydration flags — the whole point of task 89e90d1a. `data`/`act` being
  // null cannot distinguish "not fetched yet" from "fetched and genuinely
  // empty", so every render downstream painted a confident 0. These flip once
  // per source, on settle (including the error path — a failed fetch is still
  // "we know now"), and gate the skeletons below.
  const [stateLoaded, setStateLoaded] = useState(false);
  const [actLoaded, setActLoaded] = useState(false);
  const [stranded, setStranded] = useState<StrandedRow[]>([]);
  const [strandedLoaded, setStrandedLoaded] = useState(false);
  // Memory coverage (task 1edee95c): DISTINCT memories the Brain holds over
  // memories on file. The server counts them; this page never derives it.
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [coverageLoaded, setCoverageLoaded] = useState(false);

  const load = useCallback(() => {
    api.get<State>(`/api/dashboard/state?project=${project}`)
      .then(setData).catch(() => setData(null))
      .finally(() => setStateLoaded(true));
    api.get<Activity>(`/api/dashboard/activity?project=${project}&days=14`)
      .then(setAct).catch(() => setAct(null))
      .finally(() => setActLoaded(true));
    api.get<Coverage>(`/api/brain/health?project=${project}`)
      .then(setCoverage).catch(() => setCoverage(null))
      .finally(() => setCoverageLoaded(true));
  }, [project]);

  // Unshipped-done scan (task b22576bb): "done" that never merged to main.
  // Loaded PROGRESSIVELY on its own slow cadence, never in the 5s loop: the
  // server-side scan walks git history for every done task (measured 35s at
  // 301 done tasks, 2026-08-13) and re-firing it each poll stacked requests
  // faster than they finished, starving every other page's fetches.
  // Stranded-ness only moves on merges — 5 minutes is current.
  const loadStranded = useCallback(() => {
    api.get<{ stranded: StrandedRow[] }>(`/api/tasks/stranded?project=${project}`)
      .then((r) => setStranded(r.stranded ?? [])).catch(() => setStranded([]))
      .finally(() => setStrandedLoaded(true));
  }, [project]);

  // Only poll a tab someone is looking at (the Sidebar useStaleness
  // precedent, task c38ef597); refetch on focus so it's current when seen.
  useEffect(() => {
    const tick = () => { if (!document.hidden) load(); };
    load();
    const t = setInterval(tick, 5000);
    document.addEventListener("visibilitychange", tick);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", tick); };
  }, [load]);
  useEffect(() => {
    const tick = () => { if (!document.hidden) loadStranded(); };
    loadStranded();
    const t = setInterval(tick, 300_000);
    return () => clearInterval(t);
  }, [loadStranded]);

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

  // Is this install genuinely EMPTY, as opposed to not-fetched-yet? Derived
  // from the HYDRATED counts api/dashboard.py returns, never from `data`
  // being null, which is the "we don't know" state the skeletons cover.
  const instanceEmpty = useMemo(() => {
    const k = data?.kpis;
    if (!k) return false;
    return (k.brain_docs + k.entities + k.memories + k.tasks_active) === 0;
  }, [data]);

  const health = data?.health;
  const q = act?.queries;
  const zeroPct = q && q.total ? Math.round((q.zero / q.total) * 100) : 0;
  const gates = act ? act.flow.gate_passed + act.flow.gate_failed : 0;
  const gatePct = gates ? Math.round((act!.flow.gate_passed / gates) * 100) : null;

  return (
    <Page>
      {/* Hero — the brain's pulse over time */}
      <Card raised>
        {/* Skeleton ONLY while unhydrated; once the flags flip the real state
            must show through, genuine emptiness included (never mask it).
            Three states, not two: unknown, empty-and-new, and has-history. */}
        <SectionLabel>Brain activity · last 14 days</SectionLabel>
        {!actLoaded || !stateLoaded
          ? <Skeleton className="h-[210px] w-full" />
          : instanceEmpty
            ? <FreshInstallPanel />
            : pulse
              ? <PlotFigure options={pulse} className="w-full" />
              : <Empty>No activity yet.</Empty>}
      </Card>

      {/* Stranded work: DONE tasks whose commits are not (yet) reachable
          from origin/main (owner 2026-07-16: done means SHIPPED). Reachable
          the instant a person lands on Dashboard -- no extra click needed. */}
      <Card raised>
        <SectionLabel>Stranded work</SectionLabel>
        {!strandedLoaded ? (
          <Skeleton className="h-[92px] w-full" />
        ) : stranded.length ? (
          <ul className="space-y-1.5 mt-2">
            {stranded.map((r) => (
              <li key={r.task_id} className="flex items-center gap-2 text-sm">
                <Lozenge tone={r.state === "local_only" ? "danger" : "warn"} className="shrink-0">
                  {r.state === "local_only" ? "local only" : "unmerged"}
                </Lozenge>
                <a
                  href={`/tasks/${r.task_id}?project=${encodeURIComponent(project)}`}
                  className="truncate text-[color:var(--text-secondary)] hover:underline"
                >
                  {r.title || r.task_id}
                </a>
                <span className="ml-auto shrink-0 text-2xs text-[color:var(--text-muted)] tabular-nums">
                  {r.commits_ahead} commit{r.commits_ahead === 1 ? "" : "s"} ahead
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <Empty>Every finished task has landed on main. Nothing here is waiting to ship.</Empty>
        )}
      </Card>

      <StalenessCard project={project} nothingIndexed={instanceEmpty} />

      {/* Trend KPIs — movement, not static inventory */}
      <section className="flex flex-wrap gap-3">
        {actLoaded ? (
          <>
            <TrendKpi label="Queries" series={act?.series.searches ?? []} color={TONE.teal} />
            <TrendKpi label="Docs indexed" series={act?.series.indexing ?? []} color={TONE.sage} />
            <TrendKpi label="Workflow events" series={act?.series.workflow ?? []} color={TONE.violet} />
            <TrendKpi label="Tasks shipped" series={act?.flow.completed ?? []} color={TONE.emerald} />
            <TrendKpi label="Tokens (14d)" series={act?.tokens.per_day ?? []} color={TONE.amber} fmt={compact} />
          </>
        ) : (
          /* pre-hydration: 5 placeholders until actLoaded flips — `?? []`
             would otherwise make sum([]) paint a confident 0 on every load */
          [0, 1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-[92px] flex-1 min-w-[150px]" />
          ))
        )}
      </section>

      {/* Interactions + Delivery flow */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <SectionLabel>Interactions · search</SectionLabel>
          {!actLoaded ? <Skeleton className="h-[62px] w-full mb-3" /> : (
            <div className="flex gap-3 mb-3">
              <Stat label="queries" value={nf(q?.total ?? 0)} />
              <Stat label="avg latency" value={q?.avg_latency != null ? `${q.avg_latency} ms` : "—"} />
              <Stat label="avg results" value={q ? String(q.avg_results) : "—"} tone={q && q.avg_results < 1 ? TONE.rose : undefined} />
              <Stat label="zero-result · all time" value={`${zeroPct}%`} tone={zeroPct > 50 ? TONE.rose : undefined} />
              {/* Task a91976ec: the figure to its left counts EVERY search ever
                  recorded, so a fixed outage keeps reading as a live one. This
                  names the recent window beside it. A dash, never 0%, when the
                  window holds nothing -- an unmeasured period must not render
                  as perfect health. */}
              <Stat
                label={`zero-result · last ${q?.recent_days ?? 2}d`}
                value={q?.recent_rate == null ? "—" : `${Math.round(q.recent_rate * 100)}%`}
                tone={q?.recent_rate != null && q.recent_rate > 0.5 ? TONE.rose : undefined}
              />
            </div>
          )}
          {qChart && <PlotFigure options={qChart} className="w-full mb-3" />}
          <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)] mb-2">Recent queries</div>
          {!actLoaded ? <Skeleton className="h-[92px] w-full" /> : q?.recent.length ? (
            <ul className="space-y-1.5">
              {q.recent.map((r, i) => (
                <li key={i} className="flex items-center gap-2 text-sm">
                  <Lozenge tone={r.n_results ? "ok" : "danger"} className="shrink-0 tabular-nums">{r.n_results}</Lozenge>
                  <span className="truncate text-[color:var(--text-secondary)]">{r.q}</span>
                  <span className="ml-auto shrink-0 text-2xs text-[color:var(--text-muted)]">{r.latency_ms} ms</span>
                </li>
              ))}
            </ul>
          ) : <Empty>No searches yet.</Empty>}
        </Card>

        <Card>
          <SectionLabel>Delivery flow</SectionLabel>
          {/* reads BOTH sources — skeleton until each has loaded */}
          {!(actLoaded && stateLoaded) ? <Skeleton className="h-[62px] w-full mb-3" /> : (
            <div className="flex gap-3 mb-3">
              <Stat label="gate pass-rate" value={gatePct != null ? `${gatePct}%` : "—"} tone={gatePct === 100 ? TONE.emerald : gatePct != null ? TONE.amber : undefined} />
              <Stat label="cycle time" value={act?.flow?.cycle_days != null ? `${act.flow.cycle_days} d` : "—"} />
              <Stat label="shipped" value={nf(sum(act?.flow?.completed ?? []))} />
              <Stat label="active" value={nf(data?.kpis?.tasks_active ?? 0)} />
            </div>
          )}
          {flowChart && <PlotFigure options={flowChart} className="w-full mb-3" />}
          <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)] mb-1">Workflow events by action</div>
          {!actLoaded
            ? <Skeleton className="h-[92px] w-full" />
            : eventsChart ? <PlotFigure options={eventsChart} className="w-full" /> : <Empty>No events.</Empty>}
        </Card>
      </div>

      {/* Token usage + Governance */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-2">
          <SectionLabel>Token usage · per work session</SectionLabel>
          {!actLoaded ? <Skeleton className="h-[62px] w-full mb-3" /> : (
            <div className="flex gap-3 mb-3">
              <Stat label="total tokens" value={compact(act?.tokens.total ?? 0)} tone={TONE.amber} />
              <Stat label="avg / session" value={compact(act?.tokens.avg_session ?? 0)} />
              <Stat label="sessions" value={nf(act?.tokens.sessions ?? 0)} />
              <Stat label="last 14 days" value={compact(act?.tokens.window_total ?? 0)} />
            </div>
          )}
          {!actLoaded
            ? <Skeleton className="h-[150px] w-full" />
            : tokenChart ? <PlotFigure options={tokenChart} className="w-full" /> : <Empty>No token data yet.</Empty>}
          <div className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] mt-2">Tracked per session — PRISM does not record tokens per individual task.</div>
        </Card>

        <Card>
          <SectionLabel>Governance</SectionLabel>
          {!stateLoaded ? <Skeleton className="h-[132px] w-full" /> : health ? (
            <div className="space-y-2 text-sm">
              <Row label="Flagged conflicts" v={health.flagged_conflicts} bad={health.flagged_conflicts > 0} />
              <Row label="Stuck tasks" v={health.stuck_tasks} bad={health.stuck_tasks > 0} />
              <Row label="Stale brain docs" v={health.stale_brain_docs} bad={health.stale_brain_docs > 0} />
              <Row label="Domains near cap" v={health.domains_near_cap.length} bad={health.domains_near_cap.length > 0} />
              {!coverageLoaded ? <Skeleton className="h-[20px] w-full" /> : coverage ? (
                <>
                  <div className="flex items-center justify-between">
                    <span className="opacity-80">Memory coverage</span>
                    <Lozenge tone={coverage.ratio < 1 ? "warn" : "ok"} className="tabular-nums">
                      {`${coverage.indexed} / ${coverage.entries} (${Math.round(coverage.ratio * 100)}%)`}
                    </Lozenge>
                  </div>
                  {coverage.history && coverage.history.length > 1 ? (
                    <CoverageTrend series={coverage.history} />
                  ) : null}
                </>
              ) : null}
              {health.last_governance_run && (
                <div className="text-2xs uppercase tracking-wider opacity-50 pt-2">Last run: {health.last_governance_run}</div>
              )}
            </div>
          ) : <Empty>—</Empty>}
        </Card>
      </div>
    </Page>
  );
}
