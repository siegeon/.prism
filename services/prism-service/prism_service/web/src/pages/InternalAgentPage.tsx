/**
 * Internal agent audit view — /internal-agent.
 *
 * Every internal pi/local inference run (pi_agent jobs incl. reflection
 * backend=pi, local_llm completions) lands in the pi_run_log ledger;
 * this page renders it: KPI strip (runs today, avg ms, tokens today,
 * error rate) + newest-first run rows. A row click expands the
 * tools_used receipts as one-line rows (progressive disclosure — never
 * a wall of JSON). 10s poll + /sse/sessions refresh nudge.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Card, Empty, Kpi, Page, SectionLabel } from "@/components/ui";

type ToolReceipt = { name: string; ms: number; ok: boolean };

type PiRun = {
  run_id: string;
  ts: number;
  duration_ms: number;
  backend: string; // "pi" | "local"
  model: string;
  purpose: string; // "reflect" | "panel-bridge" | "adhoc"
  project: string;
  prompt_chars: number;
  tools_used: ToolReceipt[];
  turns: number;
  tokens: number;
  ok: boolean;
  error: string;
};

const BACKEND_TONE: Record<string, string> = {
  pi: "bg-[color:var(--accent-violet-bg)] text-[color:var(--accent-violet-fg)] ring-1 ring-inset ring-[color:var(--accent-violet-ring)]",
  local: "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)] ring-1 ring-inset ring-[color:var(--accent-teal-ring)]",
};

function fmtMs(ms: number): string {
  if (!ms) return "0ms";
  return ms >= 10_000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

function fmtWhen(epochS: number): string {
  const d = new Date(epochS * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return sameDay ? hm : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${hm}`;
}

function RunRow({ run }: { run: PiRun }) {
  const [open, setOpen] = useState(false);
  const receipts = run.tools_used ?? [];
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="py-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-3 text-left text-sm hover:bg-[color:var(--surface-2)] rounded-md px-2 py-1.5 transition-colors"
        title={open ? "Collapse run detail" : "Expand run detail"}
      >
        <Chevron className="w-3.5 h-3.5 shrink-0 text-[color:var(--text-muted)]" />
        <span className="text-[11px] font-mono text-[color:var(--text-muted)] whitespace-nowrap w-[86px]">
          {fmtWhen(run.ts)}
        </span>
        <span
          className={`px-2 py-0.5 rounded-full text-[10px] uppercase tracking-wider whitespace-nowrap ${
            BACKEND_TONE[run.backend] ?? "bg-[color:var(--surface-2)] text-[color:var(--text-secondary)]"
          }`}
        >
          {run.backend || "?"}
        </span>
        <span className="font-mono text-[11px] px-2 py-0.5 rounded bg-[color:var(--surface-2)] text-[color:var(--text-secondary)] truncate max-w-[180px]">
          {run.model || "—"}
        </span>
        <span className="text-[11px] uppercase tracking-wider text-[color:var(--text-secondary)] whitespace-nowrap flex-1 truncate">
          {run.purpose || "adhoc"}
        </span>
        {receipts.length > 0 && (
          <span className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] whitespace-nowrap">
            {receipts.length} tool{receipts.length === 1 ? "" : "s"}
          </span>
        )}
        <span className="text-xs font-mono text-[color:var(--text-secondary)] whitespace-nowrap w-[64px] text-right">
          {fmtMs(run.duration_ms)}
        </span>
        <span className="text-xs font-mono text-[color:var(--text-secondary)] whitespace-nowrap w-[64px] text-right">
          {run.tokens} tok
        </span>
        <span
          className={`text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full whitespace-nowrap ${
            run.ok
              ? "bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)]"
              : "bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)]"
          }`}
        >
          {run.ok ? "ok" : "error"}
        </span>
      </button>
      {open && (
        <div className="mt-1 ml-[26px] pl-3 border-l border-[color:var(--border-default)] space-y-1">
          <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] font-mono">
            run {run.run_id} · project {run.project || "—"} · {run.prompt_chars} prompt chars
            {run.turns > 0 && ` · ${run.turns} turns`}
          </div>
          {run.error && (
            <div className="text-xs font-mono text-[color:var(--accent-rose-fg)] break-all">
              {run.error}
            </div>
          )}
          {receipts.length === 0 ? (
            <div className="text-xs text-[color:var(--text-muted)]">
              no tool calls (plain completion)
            </div>
          ) : (
            receipts.map((t, i) => (
              <div key={`${run.run_id}-${i}`} className="flex items-center gap-3 text-xs">
                <span
                  className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                    t.ok
                      ? "bg-[color:var(--accent-emerald-fg)]"
                      : "bg-[color:var(--accent-rose-fg)]"
                  }`}
                  aria-label={t.ok ? "tool ok" : "tool failed"}
                />
                <span className="font-mono text-[color:var(--text-secondary)] flex-1 truncate">
                  {t.name}
                </span>
                <span className="font-mono text-[color:var(--text-muted)] w-[64px] text-right">
                  {fmtMs(t.ms)}
                </span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

export default function InternalAgentPage() {
  const [project] = useProject();
  const [runs, setRuns] = useState<PiRun[]>([]);
  const [error, setError] = useState(false);

  const load = useCallback(() => {
    api
      .get<{ runs: PiRun[] }>(`/api/pi-runs?project=${encodeURIComponent(project)}&limit=100`)
      .then((d) => { setRuns(d.runs ?? []); setError(false); })
      .catch(() => setError(true));
  }, [project]);

  // 10s poll — the ledger is file-backed and cheap to read.
  useEffect(() => {
    load();
    const t = setInterval(load, 10_000);
    return () => clearInterval(t);
  }, [load]);

  // Refresh nudge: internal runs are usually triggered by session
  // activity (reflection, panel work), so any /sse/sessions event is a
  // hint that fresh rows may exist — reload ahead of the next poll tick.
  useEffect(() => {
    const es = new EventSource(`/sse/sessions?project=${encodeURIComponent(project)}`);
    es.onmessage = () => load();
    return () => es.close();
  }, [project, load]);

  const kpis = useMemo(() => {
    const midnight = new Date();
    midnight.setHours(0, 0, 0, 0);
    const dayStart = midnight.getTime() / 1000;
    const today = runs.filter((r) => r.ts >= dayStart);
    const avgMs = runs.length
      ? runs.reduce((s, r) => s + (r.duration_ms || 0), 0) / runs.length
      : 0;
    const tokensToday = today.reduce((s, r) => s + (r.tokens || 0), 0);
    const errors = runs.filter((r) => !r.ok).length;
    const errorRate = runs.length ? Math.round((errors / runs.length) * 100) : 0;
    return { runsToday: today.length, avgMs, tokensToday, errorRate, errors };
  }, [runs]);

  return (
    <Page>
      <div className="flex flex-wrap gap-4">
        <Kpi label="Runs today" value={kpis.runsToday} hint="pi + local backends" />
        <Kpi label="Avg duration" value={fmtMs(kpis.avgMs)} hint={`across last ${runs.length} runs`} />
        <Kpi label="Tokens today" value={kpis.tokensToday.toLocaleString()} hint="completion tokens" />
        <Kpi
          label="Error rate"
          value={`${kpis.errorRate}%`}
          hint={kpis.errors ? `${kpis.errors} failed run${kpis.errors === 1 ? "" : "s"}` : "no failures"}
        />
      </div>
      <Card className="mt-4">
        <SectionLabel>Internal inference runs</SectionLabel>
        {error ? (
          <Empty>Could not load the internal-agent ledger.</Empty>
        ) : runs.length === 0 ? (
          <Empty>
            No internal agent runs recorded yet — reflection (backend=pi/local)
            and local completions will land here as they happen.
          </Empty>
        ) : (
          <div className="divide-y divide-[color:var(--border-default)]">
            {runs.map((r) => (
              <RunRow key={r.run_id} run={r} />
            ))}
          </div>
        )}
      </Card>
    </Page>
  );
}
