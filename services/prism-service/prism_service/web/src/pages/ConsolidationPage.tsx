import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Kpi, Card, SectionLabel, Empty } from "@/components/ui";
import { Lozenge } from "@/components/Lozenge";

type QueueRow = { state: string; count: number };
type SignalRollup = {
  sessions_scanned: number;
  pushbacks: number;
  bg_signals: number;
  tool_failures: number;
  memory_writes: number;
  reflections_run: number;
  memories_minted: number;
};
type NextBrief = {
  ready: boolean;
  candidate_id?: string;
  task_id?: string | null;
  session_id?: string;
  trigger?: string;
  queued_at?: string;
  mcps_available?: string[];
  response_schema_keys?: string[];
  scope?: Record<string, unknown>;
  reason?: string;
};
type DayBucket = {
  sessions: number; pushbacks: number; tool_failures: number;
  bg_signals: number; memory_writes: number; reflections: number;
};
type Trends = {
  days: string[];
  series: Record<string, DayBucket>;
};

// v6.0.12 — vertical-card sparkline tile. Label on top, SVG full
// width of the tile, totals/peak in a thin footer. Tiles arranged in
// a responsive grid (1 / 2 / 3 / 6 columns) so the strip stops eating
// vertical space and the six metrics read across in one glance.
function Sparkline({
  values, label, color = "rgb(125 211 252)", height = 40,
}: {
  values: number[]; label: string; color?: string; height?: number;
}) {
  if (values.length === 0) return null;
  const max = Math.max(1, ...values);
  const total = values.reduce((a, b) => a + b, 0);
  // viewBox so the SVG stretches to fill the tile width; the actual
  // pixel width is controlled by the parent container.
  const viewW = 100;
  const stepX = values.length > 1 ? viewW / (values.length - 1) : 0;
  const points = values
    .map((v, i) => `${i * stepX},${height - (v / max) * (height - 4) - 2}`)
    .join(" ");
  const area = `0,${height} ${points} ${viewW},${height}`;
  return (
    <div className="rounded-md border border-[color:var(--midground-base)]/10 bg-[color:var(--background-base)]/30 p-2.5 flex flex-col gap-1.5">
      <div className="text-2xs uppercase tracking-wider opacity-60">{label}</div>
      <svg
        viewBox={`0 0 ${viewW} ${height}`}
        preserveAspectRatio="none"
        className="w-full"
        style={{ height: `${height}px` }}
      >
        <polygon points={area} fill={color} opacity="0.18" />
        <polyline points={points} fill="none" stroke={color} strokeWidth="1.2" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flex items-baseline justify-between text-2xs font-mono tabular-nums">
        <span className="opacity-85">Σ {total.toLocaleString()}</span>
        <span className="opacity-40">peak {max.toLocaleString()}</span>
      </div>
    </div>
  );
}
// Mirrors columns from consolidation_candidates (see consolidation_data.
// get_unreflected_briefs). The page formerly assumed { brief_id,
// age_hours, retry_count } — neither of the first two are real columns.
type SignalCounts = {
  pushbacks?: number;
  bg_signals?: number;
  tool_failures?: number;
  memory_writes?: number;
};
type Brief = {
  id: string;
  task_id?: string | null;
  trigger?: string;
  queued_at?: string;
  last_nudged_at?: string | null;
  retry_count?: number;
  // v6.0.5 — signal extraction from claude_transcripts. Old candidates
  // serve empty values and just render no badges / no excerpt.
  signal_counts?: SignalCounts;
  transcript_excerpt?: string;
  // v6.2.18 — set on legacy no-task / zero-signal rows so the row can
  // show WHY it's empty (noise the enqueue filter now blocks going forward).
  skip_reason?: string;
};
type Run = {
  id: string;
  candidate_id?: string;
  run_at?: string;
  output_json?: string;
  subagent_type?: string;
  confidence?: number;
  narrative_excerpt?: string;
};

// The /api/consolidation endpoint returns `queue` as an object keyed
// by state ({pending: N, completed: N, ...}), not an array. The page
// renders one Kpi per state, so we flatten to QueueRow[] on receipt
// instead of guarding every render site against the object shape.
function normalizeQueue(q: unknown): QueueRow[] {
  if (Array.isArray(q)) return q as QueueRow[];
  if (q && typeof q === "object") {
    return Object.entries(q as Record<string, number>)
      .map(([state, count]) => ({ state, count: Number(count) || 0 }));
  }
  return [];
}

// Structured render of the brief scope (was a raw JSON.stringify blob): an
// indented key/value tree, recursing into nested objects/arrays. Leaves render
// as readable mono values, never a JSON dump.
function ScopeTree({ value, depth = 0 }: { value: unknown; depth?: number }): React.ReactElement {
  if (value === null || value === undefined) {
    return <span className="opacity-40 text-2xs font-mono">—</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="opacity-40 text-2xs font-mono">[]</span>;
    return (
      <ul className="space-y-0.5">
        {value.map((v, i) => (
          <li key={i} className="flex gap-2">
            <span className="opacity-30 text-2xs font-mono select-none">–</span>
            <ScopeTree value={v} depth={depth + 1} />
          </li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object") {
    return (
      <div className={depth > 0 ? "pl-3 border-l border-[color:var(--midground-base)]/10" : ""}>
        {Object.entries(value as Record<string, unknown>).map(([k, v]) => (
          <div key={k} className="flex gap-2 items-baseline flex-wrap py-0.5">
            <span className="text-2xs font-mono opacity-50 shrink-0">{k}:</span>
            <ScopeTree value={v} depth={depth + 1} />
          </div>
        ))}
      </div>
    );
  }
  return <span className="text-2xs font-mono opacity-85 break-words">{String(value)}</span>;
}

export default function ConsolidationPage() {
  const [project] = useProject();
  const [queue, setQueue] = useState<QueueRow[]>([]);
  const [unreflected, setUnreflected] = useState<Brief[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [rollup, setRollup] = useState<SignalRollup | null>(null);
  const [trends, setTrends] = useState<Trends | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<NextBrief | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [reflectBusy, setReflectBusy] = useState<string | null>(null);
  // v6.0.13 — visible progress for the 30-90s claude run. Tracks
  // wall-clock seconds since the click + the most recent verdict per
  // candidate so the row stays informative after completion (toast
  // alone disappears in 6s).
  const [reflectStartedAt, setReflectStartedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [verdicts, setVerdicts] = useState<Record<string, {
    qualitative_score?: number;
    confidence?: number;
    narrative?: string;
    memories_stored?: { id: string; name: string; domain: string }[];
    duration_s?: number;
    error?: string;
  }>>({});
  const [showHelp, setShowHelp] = useState(false);

  useEffect(() => {
    if (reflectStartedAt === null) {
      setElapsed(0);
      return;
    }
    const t = setInterval(() => {
      setElapsed(Math.floor((Date.now() - reflectStartedAt) / 1000));
    }, 250);
    return () => clearInterval(t);
  }, [reflectStartedAt]);

  const runReflection = async (candidateId: string) => {
    setReflectBusy(candidateId);
    setReflectStartedAt(Date.now());
    setNotice(`Reflecting on ${candidateId.slice(0, 8)}… claude is reading PRISM source. Typical 30-90s.`);
    try {
      const r = await api.post<{
        ok: boolean; error?: string;
        verdict?: { qualitative_score?: number; confidence?: number; narrative?: string };
        memories_stored?: { id: string; name: string; domain: string }[];
        run_id?: string; duration_s?: number;
      }>(`/api/consolidation/run-reflection?candidate_id=${candidateId}&project=${project}`, {});
      if (!r.ok) {
        setNotice(`Reflection failed: ${r.error ?? "unknown error"}`);
        setVerdicts((v) => ({ ...v, [candidateId]: { error: r.error ?? "unknown" } }));
      } else {
        const storedN = (r.memories_stored ?? []).length;
        setNotice(
          `Reflection complete in ${r.duration_s ?? "?"}s. Stored ${storedN} new memor${storedN === 1 ? "y" : "ies"}.`,
        );
        setVerdicts((v) => ({
          ...v, [candidateId]: {
            qualitative_score: r.verdict?.qualitative_score,
            confidence: r.verdict?.confidence,
            narrative: r.verdict?.narrative,
            memories_stored: r.memories_stored ?? [],
            duration_s: r.duration_s,
          },
        }));
        load();
      }
    } catch (e) {
      setNotice(`Reflection error: ${(e as Error).message ?? e}`);
      setVerdicts((v) => ({ ...v, [candidateId]: { error: String(e) } }));
    } finally {
      setReflectBusy(null);
      setReflectStartedAt(null);
    }
  };

  const previewNext = async () => {
    setPreviewBusy(true);
    try {
      const r = await api.get<NextBrief>(
        `/api/consolidation/next-brief?project=${project}`,
      );
      setPreview(r);
    } catch (e) {
      setNotice(`Preview failed: ${(e as Error).message ?? e}`);
    } finally {
      setPreviewBusy(false);
    }
  };

  const load = () => {
    api.get<{
      queue: unknown; unreflected: Brief[]; recent_runs: Run[];
      signal_rollup?: SignalRollup; trends?: Trends;
    }>(
      `/api/consolidation?project=${project}`,
    ).then((d) => {
      setQueue(normalizeQueue(d.queue));
      setUnreflected(d.unreflected ?? []);
      setRuns(d.recent_runs ?? []);
      setRollup(d.signal_rollup ?? null);
      setTrends(d.trends ?? null);
    })
     .catch(() => {
       setQueue([]); setUnreflected([]); setRuns([]);
       setRollup(null); setTrends(null);
     });
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [project]);

  // Auto-dismiss the notice 5s after it lands so it doesn't stick.
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(t);
  }, [notice]);

  const triggerBackfill = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ created: number; skipped: number; scanned: number }>(
        `/api/consolidation/backfill?project=${project}`, {},
      );
      setNotice(
        `Backfill scanned ${r.scanned} sessions — created ${r.created} new candidate${r.created === 1 ? "" : "s"}, ${r.skipped} already enqueued.`,
      );
      load();
    } catch (e) {
      setNotice(`Backfill failed: ${(e as Error).message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Page>
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm opacity-60 flex-1">
          Reflection queue — one <button onClick={() => setShowHelp(!showHelp)} className="underline decoration-dotted underline-offset-2 hover:opacity-100">brief</button> per session, dispensed to a
          sub-agent for post-hoc review. Populated by the Stop hook on
          session end; backfill if the hook hasn't run yet.
        </p>
        <button
          onClick={triggerBackfill}
          disabled={busy}
          className="px-4 py-2 rounded-md bg-[color:var(--midground-base)] text-[color:var(--background-base)] text-xs uppercase tracking-wider disabled:opacity-40 shrink-0"
        >
          {busy ? "Working…" : "Backfill from sessions"}
        </button>
      </div>

      {showHelp && (
        <Card>
          <div className="flex items-start justify-between gap-3">
            <div className="text-sm leading-relaxed space-y-2 flex-1">
              <div><strong>A brief</strong> is the work-packet PRISM hands to a reflection sub-agent. One brief per Claude Code session, queued automatically by the transcript importer (every 60s).</div>
              <div>Each brief carries:</div>
              <ul className="ml-5 list-disc opacity-80 space-y-0.5">
                <li><code className="opacity-90">session_id</code> + <code className="opacity-90">task_id</code> + <code className="opacity-90">trigger</code> — what triggered the queue (session_completed / transcript_imported / task_done)</li>
                <li><code className="opacity-90">signal_counts</code> — pushbacks, tool failures, bg-protocol lines, memory-store call sites extracted from the JSONL</li>
                <li><code className="opacity-90">transcript_excerpt</code> — the actual session content (~3.5KB max, wrapped in &lt;untrusted&gt; tags)</li>
              </ul>
              <div>Click <strong>Reflect</strong> on a row and PRISM spawns <code className="opacity-90">claude -p</code> headless against the PRISM source tree (Read / Glob / Grep, max_turns=15). The agent reads the brief, investigates the code, and returns a JSON verdict that:</div>
              <ul className="ml-5 list-disc opacity-80 space-y-0.5">
                <li>scores the session (qualitative_score 0-1, confidence 0-1)</li>
                <li>writes a ~200-word narrative</li>
                <li>proposes <code className="opacity-90">new_memories</code> citing real file paths — these auto-store as ExpertiseEntry rows on /memory</li>
              </ul>
              <div className="text-xs opacity-60 pt-1">Typical run: 30-90s. Each click spends real claude tokens on your subscription.</div>
            </div>
            <button onClick={() => setShowHelp(false)} className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100 shrink-0">close</button>
          </div>
        </Card>
      )}

      {notice && (
        <div className="fixed bottom-6 right-6 z-40 max-w-[420px] rounded-md border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)]/95 backdrop-blur-sm shadow-lg px-4 py-3 text-[12px] flex items-start gap-3">
          <span className="flex-1 opacity-90 leading-relaxed">{notice}</span>
          <button
            onClick={() => setNotice(null)}
            className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100 shrink-0"
          >
            dismiss
          </button>
        </div>
      )}

      {/* v6.0.6 — "what are we actually learning?" headline. Reads from
          /api/consolidation.signal_rollup. The reflections-run counter
          on the right is the punchline: pre-reflection, signals pile
          up here but nothing reaches /learning or /memory yet. */}
      {rollup && rollup.sessions_scanned > 0 && (
        <Card>
          <SectionLabel>What we've extracted</SectionLabel>
          <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mt-2">
            <Kpi label="Sessions scanned" value={rollup.sessions_scanned} />
            <Kpi label="Pushbacks" value={rollup.pushbacks} />
            <Kpi label="Tool failures" value={rollup.tool_failures} />
            <Kpi label="Bg signals" value={rollup.bg_signals} />
            <Kpi label="Memory triggers" value={rollup.memory_writes} />
            <Kpi label="Reflections run" value={rollup.reflections_run} />
          </div>
          <p className="text-xs opacity-60 mt-3 leading-relaxed">
            {rollup.reflections_run === 0 ? (
              <>
                <span className="text-[color:var(--accent-amber-fg)]">
                  {(rollup.pushbacks + rollup.tool_failures + rollup.bg_signals + rollup.memory_writes).toLocaleString()} signals
                </span>{" "}
                are queued but no reflection has run yet — none of this
                has been promoted into <code className="opacity-80">/memory</code> or scored on{" "}
                <code className="opacity-80">/learning</code>. Wire up the
                reflection sub-agent to close the loop.
              </>
            ) : (
              <>
                {rollup.reflections_run.toLocaleString()} reflection
                {rollup.reflections_run === 1 ? "" : "s"} completed —
                check <code className="opacity-80">/learning</code> for
                scored outcomes and <code className="opacity-80">/memory</code>{" "}
                for new entries.
              </>
            )}
          </p>
        </Card>
      )}

      {trends && trends.days.length > 0 && (
        <Card>
          <SectionLabel>Last 14 days (UTC)</SectionLabel>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 mt-2">
            <Sparkline label="Sessions"      values={trends.days.map((d) => trends.series[d]?.sessions ?? 0)}      color="rgb(125 211 252)" />
            <Sparkline label="Pushbacks"     values={trends.days.map((d) => trends.series[d]?.pushbacks ?? 0)}     color="rgb(252 211 77)" />
            <Sparkline label="Tool failures" values={trends.days.map((d) => trends.series[d]?.tool_failures ?? 0)} color="rgb(251 113 133)" />
            <Sparkline label="Bg signals"    values={trends.days.map((d) => trends.series[d]?.bg_signals ?? 0)}    color="rgb(165 180 252)" />
            <Sparkline label="Memory writes" values={trends.days.map((d) => trends.series[d]?.memory_writes ?? 0)} color="rgb(110 231 183)" />
            <Sparkline label="Reflections"   values={trends.days.map((d) => trends.series[d]?.reflections ?? 0)}   color="rgb(192 132 252)" />
          </div>
          <p className="text-2xs opacity-40 mt-2 font-mono">
            {trends.days[0]} → {trends.days[trends.days.length - 1]}
          </p>
        </Card>
      )}

      <Card>
        <div className="flex items-center justify-between">
          <div>
            <SectionLabel>Next brief preview</SectionLabel>
            <p className="text-xs opacity-60 mt-1 leading-relaxed">
              Peek at the candidate the reflection sub-agent would pick up
              next — non-mutating, no tokens spent. Background worker status
              lives on <code className="opacity-80">/settings/workers</code>.
            </p>
          </div>
          <button
            onClick={previewNext}
            disabled={previewBusy}
            className="text-2xs uppercase tracking-wider px-3 py-1 rounded bg-[color:var(--midground-base)]/15 hover:bg-[color:var(--midground-base)]/30 disabled:opacity-40 shrink-0"
          >
            {previewBusy ? "loading…" : "Preview next brief"}
          </button>
        </div>

        {preview && (
          <div className="mt-4 p-3 rounded-md bg-[color:var(--midground-base)]/5 border border-[color:var(--midground-base)]/10">
            <div className="flex items-center justify-end mb-2">
              <button
                onClick={() => setPreview(null)}
                className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100"
              >
                close
              </button>
            </div>
            {!preview.ready ? (
                <div className="text-xs opacity-60">{preview.reason ?? "no pending briefs"}</div>
              ) : (
                <div className="space-y-3 text-xs">
                  <div className="grid grid-cols-2 gap-x-3 gap-y-1">
                    <div><span className="opacity-50">candidate:</span> <span className="font-mono">{preview.candidate_id?.slice(0, 8)}</span></div>
                    <div><span className="opacity-50">session:</span> <span className="font-mono">{preview.session_id?.slice(0, 8)}</span></div>
                    <div><span className="opacity-50">trigger:</span> {preview.trigger}</div>
                    <div><span className="opacity-50">queued:</span> {preview.queued_at?.slice(0, 19)}</div>
                  </div>
                  {preview.mcps_available && preview.mcps_available.length > 0 && (
                    <div>
                      <span className="opacity-50">MCPs the agent would have:</span>
                      <div className="flex flex-wrap gap-1 mt-1">
                        {preview.mcps_available.map((m) => (
                          <span key={m} className="text-2xs font-mono opacity-70 px-1.5 py-0.5 rounded bg-[color:var(--midground-base)]/10">
                            {m}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                  {preview.scope && (
                    <div>
                      <span className="opacity-50">scope (full):</span>
                      <div className="mt-1 p-2 rounded bg-[color:var(--background-base)]/40 border border-[color:var(--midground-base)]/10 max-h-96 overflow-auto">
                        <ScopeTree value={preview.scope} />
                      </div>
                    </div>
                  )}
                </div>
              )}
          </div>
        )}
      </Card>

      <section className="flex flex-wrap gap-3">
        {queue.length === 0 && <Kpi label="Queue" value="—" />}
        {queue.map((r) => <Kpi key={r.state} label={r.state} value={r.count} />)}
      </section>

      <Card>
        <SectionLabel>Pending briefs</SectionLabel>
        {unreflected.length === 0 ? (
          <Empty>Nothing pending reflection.</Empty>
        ) : (
          <div className="divide-y divide-[color:var(--midground-base)]/10">
            {unreflected.map((b) => {
              const ageH = b.queued_at
                ? (Date.now() - new Date(b.queued_at).getTime()) / 3600000
                : 0;
              const sc = b.signal_counts ?? {};
              const totalSignals =
                (sc.pushbacks ?? 0) + (sc.bg_signals ?? 0) +
                (sc.tool_failures ?? 0) + (sc.memory_writes ?? 0);
              const hasExcerpt = (b.transcript_excerpt ?? "").length > 0;
              const isOpen = expanded.has(b.id);
              const toggle = () => {
                const next = new Set(expanded);
                if (next.has(b.id)) next.delete(b.id); else next.add(b.id);
                setExpanded(next);
              };
              return (
                <div key={b.id} className="py-2">
                  <div className="flex items-center gap-4 text-sm">
                    <button
                      onClick={hasExcerpt ? toggle : undefined}
                      disabled={!hasExcerpt}
                      className={`font-mono opacity-80 flex-1 truncate text-left ${hasExcerpt ? "hover:opacity-100 cursor-pointer" : "cursor-default"}`}
                      title={b.id}
                    >
                      {hasExcerpt ? (isOpen ? "▾ " : "▸ ") : "  "}{b.id}
                    </button>
                    <span className="flex gap-1 shrink-0">
                      {(sc.pushbacks ?? 0) > 0 && (
                        <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)]" title="user pushbacks">
                          {sc.pushbacks} push
                        </span>
                      )}
                      {(sc.tool_failures ?? 0) > 0 && (
                        <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)]" title="tool failures">
                          {sc.tool_failures} fail
                        </span>
                      )}
                      {(sc.bg_signals ?? 0) > 0 && (
                        <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)]" title="result:/failed:/needs input: markers">
                          {sc.bg_signals} bg
                        </span>
                      )}
                      {(sc.memory_writes ?? 0) > 0 && (
                        <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)]" title="memory_store call sites">
                          {sc.memory_writes} mem
                        </span>
                      )}
                      {totalSignals === 0 && !b.skip_reason && (
                        <span className="text-2xs uppercase tracking-wider opacity-40">no signals</span>
                      )}
                      {b.skip_reason && (
                        <span
                          className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)]"
                          title={`${b.skip_reason}. Use “Prune noise” to clear these — nothing for reflection to learn.`}
                        >
                          noise
                        </span>
                      )}
                    </span>
                    <span className="text-xs opacity-60 w-32 truncate" title={b.trigger ?? ""}>
                      {b.trigger ?? "—"}
                    </span>
                    <span className="text-xs opacity-60 w-24 text-right">retries {b.retry_count ?? 0}</span>
                    <span className="text-xs opacity-60 w-16 text-right">{ageH.toFixed(1)}h</span>
                    {reflectBusy === b.id ? (
                      <Lozenge tone="info" className="shrink-0">
                        <span className="w-1.5 h-1.5 rounded-full bg-[color:var(--accent-teal-fg)] animate-pulse" />
                        reflecting {elapsed}s
                      </Lozenge>
                    ) : verdicts[b.id]?.error ? (
                      <button
                        onClick={() => runReflection(b.id)}
                        disabled={reflectBusy !== null}
                        className="text-2xs uppercase tracking-wider px-2 py-1 rounded bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] hover:bg-[color:var(--accent-rose-bg)] disabled:opacity-40 shrink-0"
                        title={verdicts[b.id]?.error}
                      >
                        Retry
                      </button>
                    ) : verdicts[b.id] ? (
                      <Lozenge tone="ok" className="shrink-0">
                        ✓ score {(verdicts[b.id].qualitative_score ?? 0).toFixed(2)} · {(verdicts[b.id].memories_stored ?? []).length} mem
                      </Lozenge>
                    ) : (
                      <button
                        onClick={() => runReflection(b.id)}
                        disabled={reflectBusy !== null}
                        className="text-2xs uppercase tracking-wider px-2 py-1 rounded bg-[color:var(--midground-base)]/15 hover:bg-[color:var(--midground-base)]/30 disabled:opacity-40 shrink-0"
                        title="Spawn claude -p headless against PRISM source tree (Read/Glob/Grep, max_turns=15) to score this brief and extract memories"
                      >
                        Reflect
                      </button>
                    )}
                  </div>
                  {reflectBusy === b.id && (
                    <div className="ml-7 mt-2 p-2 rounded-md bg-sky-500/10 border border-sky-500/20 text-2xs flex items-center gap-3">
                      <svg className="animate-spin w-3 h-3 text-sky-300" viewBox="0 0 24 24" fill="none">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4zm2 5.3A8 8 0 014 12H0c0 3 1.1 5.8 3 7.9l3-2.6z" />
                      </svg>
                      <div className="flex-1 opacity-80">
                        claude is reading PRISM source… typical 30-90s · elapsed <span className="font-mono tabular-nums">{elapsed}s</span>
                      </div>
                    </div>
                  )}
                  {verdicts[b.id] && !verdicts[b.id].error && !reflectBusy && (
                    <div className="ml-7 mt-2 p-3 rounded-md bg-[color:var(--accent-emerald-bg)] border border-[color:var(--accent-emerald-ring)] space-y-2 text-[12px]">
                      <div className="flex items-baseline gap-4 text-2xs">
                        <span><span className="opacity-60">score:</span> <span className="font-mono">{(verdicts[b.id].qualitative_score ?? 0).toFixed(2)}</span></span>
                        <span><span className="opacity-60">confidence:</span> <span className="font-mono">{(verdicts[b.id].confidence ?? 0).toFixed(2)}</span></span>
                        <span><span className="opacity-60">duration:</span> <span className="font-mono">{verdicts[b.id].duration_s}s</span></span>
                      </div>
                      {verdicts[b.id].narrative && (
                        <div className="text-[12px] leading-relaxed opacity-90">{verdicts[b.id].narrative}</div>
                      )}
                      {(verdicts[b.id].memories_stored ?? []).length > 0 && (
                        <div className="text-2xs">
                          <div className="opacity-60 uppercase tracking-wider mb-1">memories stored ({verdicts[b.id].memories_stored?.length})</div>
                          <ul className="space-y-0.5">
                            {verdicts[b.id].memories_stored?.map((m) => (
                              <li key={m.id} className="font-mono opacity-85">
                                <span className="opacity-50">{m.domain}/</span>{m.name}
                                <span className="opacity-40 ml-2">{m.id}</span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}
                  {verdicts[b.id]?.error && !reflectBusy && (
                    <div className="ml-7 mt-2 p-2 rounded-md bg-[color:var(--accent-rose-bg)] border border-[color:var(--accent-rose-ring)] text-2xs text-[color:var(--accent-rose-fg)]">
                      {verdicts[b.id].error}
                    </div>
                  )}
                  {isOpen && hasExcerpt && (
                    <div className="mt-2 ml-4 p-3 rounded-md bg-[color:var(--midground-base)]/5 border border-[color:var(--midground-base)]/10 text-2xs leading-relaxed whitespace-pre-wrap font-mono opacity-80 max-h-96 overflow-auto">
                      {b.transcript_excerpt}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <Card>
        <SectionLabel>Recent reflection runs</SectionLabel>
        {runs.length === 0 ? (
          <Empty>No runs yet.</Empty>
        ) : (
          <div className="divide-y divide-[color:var(--midground-base)]/10">
            {runs.map((r) => (
              <div key={r.id} className="py-3 flex items-start gap-4 text-sm">
                <span className="font-mono opacity-70 text-xs w-44 shrink-0">{r.run_at ?? ""}</span>
                <span className="text-xs uppercase tracking-wider opacity-70 w-24 shrink-0">
                  {r.subagent_type ?? "—"}
                </span>
                {r.narrative_excerpt && (
                  <span className="opacity-80 flex-1">{r.narrative_excerpt}</span>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>
    </Page>
  );
}
