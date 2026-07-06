import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty } from "@/components/ui";
import { fmtTokens } from "@/lib/format";
import { useRoles } from "@/lib/useRoles";
import { domainTone, toneClass } from "@/lib/domainTone";

// Scored-task row — real get_learning_rows columns (learning_data.py). The old
// binding read composite_score/layer_a_score, which the API never emits, so
// every score rendered "—". Bind to what the wire actually carries.
type Row = {
  task_id?: string;
  qualitative_score?: number | null;
  cuped_score?: number | null;
  quality_score?: number | null;
  components_json?: string | null;
  scored_at?: string;
};

// Variant row — real get_variant_performance columns. Was mis-bound to the
// non-existent variant/mean_score keys.
type Variant = {
  prompt_id?: string;
  persona?: string;
  avg_score?: number | null;
  n?: number;
  correlational?: boolean;
};

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

// Run-length collapse of adaptive-policy history: the tuner writes a row every
// worker tick even in steady state, so the raw list is ~all "no signal moved
// the knobs". Fold consecutive identical-knob rows into one steady summary and
// keep only the ticks where a knob actually MOVED, carrying their rationale.
type PolicyGroup = { moved: boolean; rows: PolicyHistory[]; rationale?: string };
function collapsePolicyHistory(hist: PolicyHistory[]): PolicyGroup[] {
  const key = (r: PolicyHistory) =>
    `${r.forget_cutoff}|${r.decay_weight}|${r.merge_similarity_threshold}`;
  const groups: PolicyGroup[] = [];
  hist.forEach((r, i) => {
    const moved = i > 0 && key(r) !== key(hist[i - 1]);
    const last = groups[groups.length - 1];
    if (moved) groups.push({ moved: true, rows: [r], rationale: r.rationale });
    else if (last && !last.moved) last.rows.push(r);
    else groups.push({ moved: false, rows: [r] });
  });
  return groups;
}

// components_json is the per-task score-breakdown blob (scoring_service.py);
// null on most rows — render an honest note, not an empty receipt.
function ScoreReceipt({ raw }: { raw?: string | null }) {
  const parsed = useMemo(() => {
    if (!raw) return null;
    try {
      return JSON.parse(raw) as Record<string, unknown>;
    } catch {
      return null;
    }
  }, [raw]);
  if (!parsed) {
    return (
      <div className="text-[11px] opacity-40 pl-2">
        no components recorded for this score
      </div>
    );
  }
  return (
    <div className="pl-2 space-y-0.5">
      {Object.entries(parsed).map(([k, v]) => (
        <div key={k} className="flex justify-between text-[11px] font-mono">
          <span className="opacity-70">{k}</span>
          <span className="opacity-90">{String(v)}</span>
        </div>
      ))}
    </div>
  );
}

export default function LearningPage() {
  const [project] = useProject();
  // Single source of truth for agent roles (/api/roles). Joins the per-role
  // token ledger below to human labels + tier + effort. Degrades to raw ids if
  // the endpoint 404s.
  const { roleMeta } = useRoles();
  const [rows, setRows] = useState<Row[]>([]);
  const [variants, setVariants] = useState<Variant[]>([]);
  const [reflections, setReflections] = useState<Reflection[]>([]);
  const [agentAgg, setAgentAgg] = useState<AgentRunAggregates>({});
  const [policy, setPolicy] = useState<PolicyResponse>({});
  // Editable Tier-3 knobs (set / revert) — drafts overlay the active values.
  const [knobDraft, setKnobDraft] = useState<Record<string, string>>({});
  const [knobBusy, setKnobBusy] = useState(false);
  const [knobNote, setKnobNote] = useState<string | null>(null);
  // Receipt disclosure (task_id → open) and collapsed-history disclosure.
  const [openReceipt, setOpenReceipt] = useState<Record<string, boolean>>({});
  const [showHistory, setShowHistory] = useState(false);

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
    // Tier-3 adaptive policy: current tuned knobs + history + op accuracy.
    api
      .get<PolicyResponse>(`/api/learning/policy?project=${project}`)
      .then((d) => setPolicy(d ?? {}))
      .catch(() => setPolicy({}));
  }, [project]);

  const lowN = variants.length > 0 && variants.every((v) => (v.n ?? 0) < 20);
  const overridePct = Math.round((agentAgg.override_rate ?? 0) * 100);
  const knobs = policy.knobs ?? {};
  const opAccuracy = policy.op_accuracy ?? [];
  const history = policy.history ?? [];
  const groups = useMemo(() => collapsePolicyHistory(history), [history]);
  const lastMove = useMemo(
    () => [...groups].reverse().find((g) => g.moved),
    [groups],
  );

  return (
    <Page>
      {/* HERO — lead with the narrative: what the system reflected on and the
          memories it minted, not six mono-font telemetry tables. */}
      <div>
        <div className="text-lg font-semibold text-[color:var(--text-primary)]">
          What PRISM has learned
        </div>
        <div className="text-[13px] text-[color:var(--text-secondary)] mt-1 max-w-[760px]">
          The reflections the system minted into memory, the outcome scores
          driving them, and the knobs it self-tuned — leading with the narrative.
        </div>
      </div>

      <Card raised>
        <div className="flex items-baseline justify-between mb-3">
          <SectionLabel>Recent reflections · task → memories minted</SectionLabel>
          <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)]">
            the hero signal · from consolidation_runs
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
                    {(r.memories_minted ?? 0) > 0 ? (
                      <span className="text-[10px] uppercase tracking-wider text-[color:var(--accent-emerald-fg)]">
                        +{r.memories_minted} mem
                      </span>
                    ) : (
                      <span className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)]">
                        no memory minted
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

      {/* Adaptive policy — editable knobs, per-op verdict accuracy, and the
          history wall collapsed to the last knob that moved + a toggle. */}
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
        {/* Collapsed history: one line for the last knob that moved (or a
            steady-state note), with the full collapsed trail behind a toggle —
            never the raw wall of identical no-op ticks. */}
        <div className="mt-3 pt-2 border-t border-[color:var(--border-default)]/30">
          {history.length === 0 ? (
            <div className="text-[11px] opacity-50">
              No tunings yet — knobs show defaults until the adaptive loop runs.
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between text-[11px] gap-2">
                <span className="text-[color:var(--text-secondary)] min-w-0 truncate">
                  {lastMove ? (
                    <>
                      last knob move ·{" "}
                      <span className="text-[color:var(--accent-violet-fg)]">
                        {lastMove.rows[0]?.tuned_at}
                      </span>{" "}
                      — {lastMove.rationale}
                    </>
                  ) : (
                    <span className="opacity-60">
                      steady — no knob has moved across the last {history.length} ticks
                    </span>
                  )}
                </span>
                <button
                  type="button"
                  onClick={() => setShowHistory((s) => !s)}
                  className="text-[10px] uppercase tracking-wider opacity-60 hover:opacity-100 flex-none"
                >
                  {showHistory ? "hide" : `history · ${history.length} ticks`}
                </button>
              </div>
              {showHistory && (
                <div className="mt-2 space-y-1">
                  {groups.map((g, gi) =>
                    g.moved ? (
                      <div
                        key={gi}
                        className="text-[11px] rounded border px-2 py-1 border-[color:var(--accent-violet-ring)] bg-[color:var(--accent-violet-bg)]"
                      >
                        <span className="uppercase tracking-wider text-[color:var(--accent-violet-fg)]">
                          knob moved
                        </span>{" "}
                        <span className="opacity-60">{g.rows[0]?.tuned_at}</span>
                        <div className="opacity-80 mt-0.5">{g.rationale}</div>
                      </div>
                    ) : (
                      <div key={gi} className="text-[11px] opacity-50">
                        steady since {g.rows[0]?.tuned_at} · {g.rows.length} identical
                        tick{g.rows.length > 1 ? "s" : ""}
                      </div>
                    ),
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </Card>

      {/* Scored tasks — bound to real columns (qualitative headline, cuped/
          quality adjusted) with a click-to-expand components receipt. */}
      <Card>
        <div className="flex items-baseline justify-between mb-3">
          <SectionLabel>Scored tasks</SectionLabel>
          <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)]">
            qualitative headline · cuped/quality adjusted · click a row for the receipt
          </div>
        </div>
        {rows.length === 0 ? (
          <Empty>
            No scored tasks yet. Only candidates with a task_id fill here —
            transcript-derived reflections appear in the hero panel above.
          </Empty>
        ) : (
          <div className="divide-y divide-[color:var(--border-default)]/40">
            {rows.map((r, i) => {
              const id = r.task_id ?? String(i);
              const adjusted = r.cuped_score ?? r.quality_score;
              const open = !!openReceipt[id];
              return (
                <div key={id} className="py-2">
                  <div
                    className="flex items-center gap-4 text-sm cursor-pointer select-none"
                    onClick={() => setOpenReceipt((s) => ({ ...s, [id]: !open }))}
                  >
                    <ScorePill value={r.qualitative_score} />
                    <span className="font-mono opacity-80 flex-1 truncate">{r.task_id ?? "—"}</span>
                    <span className="text-xs opacity-60 w-40 text-right">
                      adjusted{" "}
                      <span className="font-mono text-[color:var(--text-secondary)]">
                        {adjusted != null ? adjusted.toFixed(2) : "—"}
                      </span>{" "}
                      {r.cuped_score != null ? "(cuped)" : "(quality)"}
                    </span>
                    <span className="text-xs opacity-50 w-40 text-right">{r.scored_at ?? ""}</span>
                    <span className="opacity-40 w-4 text-right">{open ? "▾" : "▸"}</span>
                  </div>
                  {open && (
                    <div className="mt-2 rounded border border-[color:var(--border-default)]/40 p-2">
                      <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] mb-1">
                        components_json — what moved the score
                      </div>
                      <ScoreReceipt raw={r.components_json} />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>

      {/* Variant performance — bound to prompt_id/persona/avg_score. */}
      <Card>
        <SectionLabel>Variant performance</SectionLabel>
        {variants.length === 0 ? (
          <Empty>No variant scores yet — needs scored task outcomes across prompt variants.</Empty>
        ) : (
          <>
            {lowN && (
              <div className="mb-3 text-[11px] uppercase tracking-wider text-[color:var(--accent-amber-fg)]">
                Correlational only — every variant has n &lt; 20.
              </div>
            )}
            <div className="divide-y divide-[color:var(--border-default)]/40">
              {variants.map((v) => (
                <div key={`${v.prompt_id}::${v.persona}`} className="py-2 flex items-center gap-4 text-sm">
                  <span className="font-mono opacity-80 flex-1 truncate">
                    {v.prompt_id ?? "—"}
                    {v.persona && <span className="opacity-50 text-xs"> · {v.persona}</span>}
                  </span>
                  {v.correlational && (
                    <span className="text-[10px] uppercase tracking-wider text-[color:var(--accent-amber-fg)]">
                      correlational · n&lt;20
                    </span>
                  )}
                  <span className="text-xs opacity-60 w-16 text-right">n={v.n ?? 0}</span>
                  <span className="w-16 text-right">
                    <ScorePill value={v.avg_score} />
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
      </Card>

      {/* Agent-run aggregates only — the raw per-run timeline had null
          model/duration/tokens on every row (a telemetry gap, not data), so it
          is replaced by aggregates; null columns are hidden, not dashed. The
          per-role ledger joins the role registry for tier/effort labels. */}
      <Card raised>
        <div className="flex items-baseline justify-between mb-3">
          <SectionLabel>Agent-run aggregates</SectionLabel>
          <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)]">
            per-step / override-rate / per-role · aggregates only
          </div>
        </div>
        {(agentAgg.total_runs ?? 0) === 0 ? (
          <Empty>
            No agent runs yet — they populate as the{" "}
            <span className="font-mono">implement</span> workflow drives steps
            and the conductor records per-step telemetry.
          </Empty>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            <div className="rounded border border-[color:var(--border-default)]/40 p-2">
              <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-muted)] mb-1">
                Runs per step
              </div>
              {(agentAgg.per_step ?? []).length === 0 ? (
                <div className="text-xs opacity-50">—</div>
              ) : (
                (agentAgg.per_step ?? []).map((s) => (
                  <div key={s.step} className="flex justify-between text-xs py-0.5">
                    <span className="font-mono opacity-80 truncate">{s.step}</span>
                    <span className="font-mono opacity-60">
                      {s.avg_duration_ms != null ? fmtMs(s.avg_duration_ms) : `n=${s.n ?? 0}`}
                    </span>
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
              {/* Wargame economics: frontier tier plans & signs the gates
                  (Steward), fast tier does the bulk execution (Builder). */}
              <div className="text-[10px] text-[color:var(--text-muted)] mb-1.5 leading-snug">
                Frontier plans &amp; gates, fast executes — spend should skew to the
                cheap tier.
              </div>
              {(agentAgg.per_role ?? []).length === 0 ? (
                <div className="text-xs opacity-50">—</div>
              ) : (
                (agentAgg.per_role ?? []).map((r) => {
                  const meta = r.role ? roleMeta(r.role) : undefined;
                  const tier = meta?.tier;
                  return (
                    <div key={r.role} className="flex items-center justify-between text-xs py-1 gap-2">
                      <span className="flex items-center gap-1.5 min-w-0">
                        <span
                          className="truncate text-[color:var(--text-primary)]"
                          title={meta?.purpose}
                        >
                          {meta?.label ?? r.role ?? "—"}
                        </span>
                        {tier && (
                          <span
                            className={
                              "px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wider font-mono flex-none " +
                              toneClass(domainTone("tier", tier) ?? "slate")
                            }
                            title={meta?.tier_desc}
                          >
                            {tier}
                          </span>
                        )}
                        {meta?.effort && (
                          <span className="text-[9px] uppercase tracking-wider font-mono text-[color:var(--text-muted)] flex-none">
                            {meta.effort}
                          </span>
                        )}
                      </span>
                      <span className="font-mono opacity-60 flex-none">
                        {(r.total_tokens ?? 0) > 0 ? `${fmtTokens(r.total_tokens ?? 0)} tok` : "—"}
                      </span>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        )}
      </Card>
    </Page>
  );
}
