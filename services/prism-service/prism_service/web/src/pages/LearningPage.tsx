import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty } from "@/components/ui";
import { fmtTokens } from "@/lib/format";

type Row = {
  task_id?: string;
  composite_score?: number;
  layer_a_score?: number;
  variant?: string;
  scored_at?: string;
};

type Variant = { variant: string; n: number; mean_score: number };

type Reflection = {
  id?: number;
  candidate_id?: string;
  run_at?: string;
  subagent_type?: string;
  confidence?: number | null;
  qualitative_score?: number | null;
  narrative_excerpt?: string;
  memories_minted?: number;
  candidate_task_id?: string | null;
  candidate_session_id?: string | null;
  candidate_trigger?: string | null;
};

type AgentRun = {
  run_id?: string;
  task_id?: string;
  role?: string;
  step?: string;
  model?: string;
  duration_ms?: number | null;
  tokens?: number | null;
  gate_state?: string;
  ok?: boolean | null;
  started_at?: string;
};

type PerStepAgg = {
  step?: string;
  n?: number;
  avg_duration_ms?: number | null;
  avg_tokens?: number | null;
};
type PerRoleAgg = {
  role?: string;
  n?: number;
  total_tokens?: number | null;
  avg_tokens?: number | null;
};
type AgentRunAggregates = {
  per_step?: PerStepAgg[];
  per_role?: PerRoleAgg[];
  override_rate?: number;
  total_runs?: number;
};

// Tier-3 adaptive policy — tuned memory knobs + per-op verdict accuracy.
type PolicyKnobs = {
  forget_cutoff?: number;
  decay_weight?: number;
  merge_similarity_threshold?: number;
};
type PolicyHistory = PolicyKnobs & {
  id?: number;
  rationale?: string;
  tuned_at?: string;
};
type OpAccuracy = {
  op_type?: string;
  n?: number;
  accuracy?: number;
  decisions?: Record<string, number>;
};
type PolicyResponse = {
  knobs?: PolicyKnobs;
  history?: PolicyHistory[];
  op_accuracy?: OpAccuracy[];
};

function fmtMs(ms?: number | null): string {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

function ScorePill({ value }: { value: number | null | undefined }) {
  if (value == null || Number.isNaN(value)) {
    return <span className="font-mono opacity-60">—</span>;
  }
  // green ≥ 0.7 · amber 0.4-0.7 · rose < 0.4
  const tone =
    value >= 0.7
      ? "text-[color:var(--accent-emerald-fg)] bg-[color:var(--accent-emerald-bg)] border-[color:var(--accent-emerald-ring)]"
      : value >= 0.4
        ? "text-[color:var(--accent-amber-fg)] bg-[color:var(--accent-amber-bg)] border-[color:var(--accent-amber-ring)]"
        : "text-[color:var(--accent-rose-fg)] bg-[color:var(--accent-rose-bg)] border-[color:var(--accent-rose-ring)]";
  return (
    <span className={`font-mono text-xs px-2 py-0.5 rounded border ${tone}`}>
      {value.toFixed(2)}
    </span>
  );
}

export default function LearningPage() {
  const [project] = useProject();
  const [rows, setRows] = useState<Row[]>([]);
  const [variants, setVariants] = useState<Variant[]>([]);
  const [reflections, setReflections] = useState<Reflection[]>([]);
  const [agentRuns, setAgentRuns] = useState<AgentRun[]>([]);
  const [agentAgg, setAgentAgg] = useState<AgentRunAggregates>({});
  const [policy, setPolicy] = useState<PolicyResponse>({});
  // Editable Tier-3 knobs (set / revert) — drafts overlay the active values.
  const [knobDraft, setKnobDraft] = useState<Record<string, string>>({});
  const [knobBusy, setKnobBusy] = useState(false);
  const [knobNote, setKnobNote] = useState<string | null>(null);

  const loadPolicy = () =>
    api
      .get<PolicyResponse>(`/api/learning/policy?project=${project}`)
      .then((d) => { setPolicy(d ?? {}); setKnobDraft({}); })
      .catch(() => setPolicy({}));

  const saveKnobs = async (action: "set" | "revert") => {
    setKnobBusy(true);
    try {
      const payload: Record<string, unknown> = { action };
      if (action === "set") {
        for (const [k, v] of Object.entries(knobDraft)) {
          if (v.trim() !== "") payload[k] = Number(v);
        }
      }
      const r = await fetch(`/api/learning/policy?project=${project}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const b = await r.json().catch(() => ({}));
      setKnobNote(r.ok && b.ok !== false ? `${action} ok` : `${action} failed: ${b.detail ?? r.statusText}`);
      await loadPolicy();
    } catch (e) {
      setKnobNote(`${action} failed: ${(e as Error).message ?? e}`);
    } finally {
      setKnobBusy(false);
    }
  };

  useEffect(() => {
    api
      .get<{
        rows: Row[];
        variants: Variant[];
        recent_reflections?: Reflection[];
        agent_runs?: AgentRunAggregates;
      }>(`/api/learning?project=${project}`)
      .then((d) => {
        setRows(d.rows ?? []);
        setVariants(d.variants ?? []);
        setReflections(d.recent_reflections ?? []);
        setAgentAgg(d.agent_runs ?? {});
      })
      .catch(() => {
        setRows([]);
        setVariants([]);
        setReflections([]);
        setAgentAgg({});
      });
    // Per-task agent timeline: the raw agent-run rows (role/step/model/
    // duration/tokens), newest-first, for the timeline panel below.
    api
      .get<{ rows: AgentRun[] }>(`/api/agent-runs?project=${project}`)
      .then((d) => setAgentRuns(d.rows ?? []))
      .catch(() => setAgentRuns([]));
    // Tier-3 adaptive policy: current tuned knobs + history + op accuracy.
    api
      .get<PolicyResponse>(`/api/learning/policy?project=${project}`)
      .then((d) => setPolicy(d ?? {}))
      .catch(() => setPolicy({}));
  }, [project]);

  const lowN = variants.length > 0 && variants.every((v) => v.n < 20);

  const overridePct = Math.round((agentAgg.override_rate ?? 0) * 100);
  const knobs = policy.knobs ?? {};
  const opAccuracy = policy.op_accuracy ?? [];

  return (
    <Page>
      <Card raised>
        <div className="flex items-baseline justify-between mb-3">
          <SectionLabel>Adaptive policy</SectionLabel>
          <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)]">
            Tier-3 · knobs self-tuned from recall→outcome · per-op verdict accuracy
          </div>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          <div className="rounded border border-[color:var(--border-default)]/40 p-2">
            <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] mb-1">
              Tuned knobs (editable)
            </div>
            {([
              ["forget_cutoff", knobs.forget_cutoff],
              ["decay_weight", knobs.decay_weight],
              ["merge_similarity_threshold", knobs.merge_similarity_threshold],
            ] as const).map(([k, val]) => (
              <div key={k} className="flex justify-between items-center text-xs py-0.5 gap-2">
                <span className="font-mono opacity-80">{k}</span>
                <input
                  type="number"
                  step="0.01"
                  value={knobDraft[k] ?? (typeof val === "number" ? String(val) : "")}
                  onChange={(e) => setKnobDraft((d) => ({ ...d, [k]: e.target.value }))}
                  className="w-20 text-right font-mono text-xs rounded bg-[color:var(--surface-2)] border border-[color:var(--border-default)] px-1 py-0.5"
                />
              </div>
            ))}
            <div className="flex gap-2 mt-2">
              <button
                type="button"
                disabled={knobBusy}
                onClick={() => saveKnobs("set")}
                className="text-[10px] uppercase tracking-wider px-2 py-1 rounded disabled:opacity-40"
                style={{ background: "var(--accent-emerald-bg)", color: "var(--accent-emerald-fg)" }}
              >
                Save
              </button>
              <button
                type="button"
                disabled={knobBusy}
                onClick={() => saveKnobs("revert")}
                className="text-[10px] uppercase tracking-wider px-2 py-1 rounded bg-[color:var(--midground-base)]/15 disabled:opacity-40"
              >
                Revert
              </button>
              {knobNote && <span className="text-[10px] opacity-60 self-center">{knobNote}</span>}
            </div>
          </div>
          <div className="rounded border border-[color:var(--border-default)]/40 p-2">
            <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] mb-1">
              Per-op verdict accuracy
            </div>
            {opAccuracy.length === 0 ? (
              <div className="text-xs opacity-50">—</div>
            ) : (
              opAccuracy.map((o) => (
                <div key={o.op_type} className="flex justify-between text-xs py-0.5">
                  <span className="font-mono opacity-80">{o.op_type}</span>
                  <span className="font-mono opacity-60">
                    {o.accuracy?.toFixed?.(2) ?? "—"} · n={o.n ?? 0}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
        {(policy.history ?? []).length === 0 && (
          <div className="mt-2 text-[11px] opacity-50">
            No tunings yet — knobs show defaults until the adaptive loop runs.
          </div>
        )}
      </Card>

      <Card raised>
        <div className="flex items-baseline justify-between mb-3">
          <SectionLabel>Agent runs · timeline</SectionLabel>
          <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)]">
            per-task agent path · role / step / model / duration / tokens
          </div>
        </div>
        {/* Cross-run aggregates: avg duration per step, override rate, token
            cost per role — the Tier-3 self-heal signals. */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3 mb-4">
          <div className="rounded border border-[color:var(--border-default)]/40 p-2">
            <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] mb-1">
              Avg duration per step
            </div>
            {(agentAgg.per_step ?? []).length === 0 ? (
              <div className="text-xs opacity-50">—</div>
            ) : (
              (agentAgg.per_step ?? []).map((s) => (
                <div key={s.step} className="flex justify-between text-xs py-0.5">
                  <span className="font-mono opacity-80 truncate">{s.step}</span>
                  <span className="font-mono opacity-60">{fmtMs(s.avg_duration_ms)}</span>
                </div>
              ))
            )}
          </div>
          <div className="rounded border border-[color:var(--border-default)]/40 p-2">
            <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] mb-1">
              Override rate
            </div>
            <div className="text-2xl font-mono text-[color:var(--accent-amber-fg)]">{overridePct}%</div>
            <div className="text-[11px] opacity-50">
              {agentAgg.total_runs ?? 0} runs · blind-verifier override signal
            </div>
          </div>
          <div className="rounded border border-[color:var(--border-default)]/40 p-2">
            <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] mb-1">
              Token cost per role
            </div>
            {(agentAgg.per_role ?? []).length === 0 ? (
              <div className="text-xs opacity-50">—</div>
            ) : (
              (agentAgg.per_role ?? []).map((r) => (
                <div key={r.role} className="flex justify-between text-xs py-0.5">
                  <span className="font-mono opacity-80">{r.role ?? "—"}</span>
                  <span className="font-mono opacity-60">{fmtTokens(r.total_tokens ?? 0)} tok</span>
                </div>
              ))
            )}
          </div>
        </div>
        {agentRuns.length === 0 ? (
          <Empty>
            No agent runs yet — they populate as the{" "}
            <span className="font-mono">implement</span> workflow drives steps
            and POSTs telemetry to <span className="font-mono">/api/agent-runs/ingest</span>.
          </Empty>
        ) : (
          <div className="divide-y divide-[color:var(--border-default)]/40">
            {agentRuns.map((a, i) => (
              <div key={`${a.run_id}-${a.step}-${i}`} className="py-2 flex items-center gap-3 text-sm">
                <span className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] w-10">
                  {a.role ?? "—"}
                </span>
                <span className="font-mono opacity-80 flex-1 truncate" title={a.task_id}>
                  {a.step ?? "—"}
                </span>
                <span className="text-xs opacity-50 w-40 truncate font-mono">{a.model ?? "—"}</span>
                <span className="font-mono text-xs opacity-60 w-16 text-right">{fmtMs(a.duration_ms)}</span>
                <span className="font-mono text-xs opacity-60 w-20 text-right">{a.tokens != null ? fmtTokens(a.tokens) : "—"} tok</span>
                {a.gate_state && a.gate_state !== "none" && (
                  <span className="text-[10px] uppercase tracking-wider text-[color:var(--accent-amber-fg)] w-14">
                    {a.gate_state}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card raised>
        <div className="flex items-baseline justify-between mb-3">
          <SectionLabel>Recent reflections</SectionLabel>
          <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)]">
            from consolidation_runs · click /consolidation to run more
          </div>
        </div>
        {reflections.length === 0 ? (
          <Empty>
            No reflections yet — click <span className="font-mono">Reflect</span>{" "}
            on a row in <span className="font-mono">/consolidation</span>, or
            opt into the background worker with{" "}
            <span className="font-mono">PRISM_REFLECTION_WORKER=on</span>.
          </Empty>
        ) : (
          <div className="divide-y divide-[color:var(--border-default)]/40">
            {reflections.map((r) => {
              const subjectId =
                r.candidate_task_id || r.candidate_session_id || r.candidate_id || "—";
              const subjectKind = r.candidate_task_id
                ? "task"
                : r.candidate_session_id
                  ? "session"
                  : "candidate";
              return (
                <div key={r.id ?? r.candidate_id} className="py-3 space-y-1">
                  <div className="flex items-center gap-3 text-sm">
                    <ScorePill value={r.qualitative_score} />
                    <span className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] w-14">
                      {subjectKind}
                    </span>
                    <span className="font-mono opacity-80 flex-1 truncate" title={subjectId}>
                      {subjectId.length > 24 ? `${subjectId.slice(0, 24)}…` : subjectId}
                    </span>
                    {(r.memories_minted ?? 0) > 0 && (
                      <span className="text-[10px] uppercase tracking-wider text-[color:var(--accent-emerald-fg)]">
                        +{r.memories_minted} mem
                      </span>
                    )}
                    <span className="text-[10px] text-[color:var(--text-muted)] w-44 text-right">
                      {r.run_at ?? ""}
                    </span>
                  </div>
                  {r.narrative_excerpt && (
                    <div className="text-[12px] text-[color:var(--text-secondary)] leading-relaxed pl-2 border-l-2 border-[color:var(--border-default)]">
                      {r.narrative_excerpt}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <Card>
        <SectionLabel>Variant performance</SectionLabel>
        {variants.length === 0 ? (
          <Empty>No variant scores yet — needs scored task outcomes.</Empty>
        ) : (
          <>
            {lowN && (
              <div className="mb-3 text-[11px] uppercase tracking-wider text-[color:var(--accent-amber-fg)]">
                Correlational only — every variant has n &lt; 20.
              </div>
            )}
            <div className="divide-y divide-[color:var(--midground-base)]/10">
              {variants.map((v) => (
                <div key={v.variant} className="py-2 flex items-center gap-4 text-sm">
                  <span className="font-mono opacity-80 flex-1">{v.variant}</span>
                  <span className="text-xs opacity-60 w-20 text-right">n={v.n}</span>
                  <span className="font-mono w-24 text-right">{v.mean_score?.toFixed?.(3) ?? "—"}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </Card>

      <Card>
        <SectionLabel>Scored tasks (Layer-A)</SectionLabel>
        {rows.length === 0 ? (
          <Empty>
            No scored tasks yet. Layer-A only fills for candidates with a
            task_id — transcript-derived reflections appear in the Recent
            reflections panel above instead.
          </Empty>
        ) : (
          <div className="divide-y divide-[color:var(--midground-base)]/10">
            {rows.map((r, i) => (
              <div key={i} className="py-2 flex items-center gap-4 text-sm">
                <span className="font-mono opacity-80 flex-1 truncate">{r.task_id ?? "—"}</span>
                <span className="text-xs opacity-60 w-32 truncate">{r.variant ?? ""}</span>
                <span className="font-mono w-20 text-right">{r.composite_score?.toFixed?.(3) ?? "—"}</span>
                <span className="text-xs opacity-50 w-40 text-right">{r.scored_at ?? ""}</span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </Page>
  );
}
