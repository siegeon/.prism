import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty } from "@/components/ui";

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

function ScorePill({ value }: { value: number | null | undefined }) {
  if (value == null || Number.isNaN(value)) {
    return <span className="font-mono opacity-60">—</span>;
  }
  // emerald ≥ 0.7 · amber 0.4-0.7 · rose < 0.4 — ported off raw
  // Tailwind onto the shared --accent-{tone} tokens so the scoring
  // language matches /memory, /tasks, /consolidation chips.
  const tone = value >= 0.7 ? "emerald" : value >= 0.4 ? "amber" : "rose";
  return (
    <span
      className="font-mono text-xs px-2 py-0.5 rounded"
      style={{
        background: `var(--accent-${tone}-bg)`,
        color: `var(--accent-${tone}-fg)`,
        boxShadow: `inset 0 0 0 1px var(--accent-${tone}-ring)`,
      }}
    >
      {value.toFixed(2)}
    </span>
  );
}

export default function LearningPage() {
  const [project] = useProject();
  const [rows, setRows] = useState<Row[]>([]);
  const [variants, setVariants] = useState<Variant[]>([]);
  const [reflections, setReflections] = useState<Reflection[]>([]);

  useEffect(() => {
    api
      .get<{
        rows: Row[];
        variants: Variant[];
        recent_reflections?: Reflection[];
      }>(`/api/learning?project=${project}`)
      .then((d) => {
        setRows(d.rows ?? []);
        setVariants(d.variants ?? []);
        setReflections(d.recent_reflections ?? []);
      })
      .catch(() => {
        setRows([]);
        setVariants([]);
        setReflections([]);
      });
  }, [project]);

  const lowN = variants.length > 0 && variants.every((v) => v.n < 20);

  return (
    <Page>
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
                      <span className="text-[10px] uppercase tracking-wider" style={{ color: "var(--accent-emerald-fg)" }}>
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
              <div className="mb-3 text-[11px] uppercase tracking-wider" style={{ color: "var(--accent-amber-fg)" }}>
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
