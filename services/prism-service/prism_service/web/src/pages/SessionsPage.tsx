import { useEffect, useState, useCallback, type ReactNode } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { subscribeStream } from "@/lib/sharedStream";
import { Page, Kpi, SectionLabel, Empty } from "@/components/ui";
import { EntityChip } from "@/components/EntityChip";
import { fmtTokens } from "@/lib/format";

// Mirrors what conductor_service.get_session_outcomes() returns. The
// keys are DB columns (snake_case_with_units) — the page renders the
// scored sessions from scores.db, not raw transcripts.
type Outcome = {
  session_id?: string;
  timestamp?: string;          // ISO-ish; "recorded_at" is the same field
  recorded_at?: string;
  tokens?: number;             // alias of tokens_used
  tokens_used?: number;
  duration?: number;           // seconds (alias of duration_s)
  duration_s?: number;
  files_read?: number;
  files_modified?: number;
  skills_invoked?: number;
  tokens_per_file?: number | null;
};

// Per-event skill_usage rows from scores.db (id/session_id/skill_name/
// timestamp). Aggregated to {skill, count} on the client so a single
// API call powers both the table and the chart.
type SkillEvent = { id: number; session_id: string; skill_name: string; timestamp: string };
type SkillRow = { skill: string; count: number };

function median(xs: number[]) {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function p95(xs: number[]) {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(s.length * 0.95))];
}

const shortId = (id?: string) => (id ?? "").slice(0, 8) || "—";

// Header cell — 11px uppercase faint label, matching TasksPage's table idiom.
const Th = ({ children, className }: { children?: ReactNode; className?: string }) => (
  <th
    className={`text-2xs uppercase tracking-wider font-semibold px-3 py-2 border-b border-[color:var(--border-default)] ${className ?? "text-left"}`}
    style={{ color: "var(--text-muted)" }}
  >
    {children}
  </th>
);

export default function SessionsPage() {
  const [project] = useProject();
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const [skills, setSkills] = useState<SkillRow[]>([]);

  const load = useCallback(() => {
    api.get<{ outcomes: Outcome[]; skill_usage: SkillEvent[] }>(`/api/sessions?project=${project}&limit=50`)
      .then((d) => {
        setOutcomes(d.outcomes);
        const counts = new Map<string, number>();
        for (const ev of d.skill_usage ?? []) {
          const k = ev.skill_name || "(unknown)";
          counts.set(k, (counts.get(k) ?? 0) + 1);
        }
        setSkills([...counts.entries()]
          .map(([skill, count]) => ({ skill, count }))
          .sort((a, b) => b.count - a.count));
      })
      .catch(() => { setOutcomes([]); setSkills([]); });
  }, [project]);

  useEffect(() => {
    load();
    return subscribeStream(`/sse/sessions?project=${project}`, () => { load(); });
  }, [project, load]);

  const tokens = outcomes.map((o) => o.tokens ?? o.tokens_used ?? 0).filter(Boolean);
  // API returns duration in SECONDS (column duration_s aliased to "duration").
  const durSec = outcomes.map((o) => o.duration ?? o.duration_s ?? 0).filter(Boolean);
  const files = outcomes.map((o) => o.files_modified ?? 0).filter(Boolean);
  const totalFiles = files.reduce((a, b) => a + b, 0) || 1;
  const totalTokens = tokens.reduce((a, b) => a + b, 0);
  const fmtTs = (s?: string) =>
    s ? s.replace("T", " ").replace(/\.\d+/, "").slice(0, 19) : "—";

  return (
    <Page>
      <section className="flex flex-wrap gap-3">
        <Kpi label="Sessions" value={outcomes.length} />
        <Kpi label="Median tokens" value={fmtTokens(Math.round(median(tokens)))} />
        <Kpi label="p95 tokens" value={fmtTokens(Math.round(p95(tokens)))} />
        <Kpi label="Tokens / file" value={fmtTokens(Math.round(totalTokens / totalFiles))} />
        <Kpi label="Median duration" value={`${Math.round(median(durSec))}s`} />
      </section>

      {/* Recent sessions — the artifact table: session chip, started, and the
          per-session cost columns (tabular numerals, right-aligned). */}
      <div>
        <SectionLabel>Recent sessions</SectionLabel>
        {outcomes.length === 0 ? (
          <Empty>No session outcomes yet.</Empty>
        ) : (
          <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr>
                  <Th>Session</Th>
                  <Th className="text-left w-52">Started</Th>
                  <Th className="text-right w-20">Duration</Th>
                  <Th className="text-right w-24">Tokens</Th>
                  <Th className="text-right w-28">Files r/w</Th>
                  <Th className="text-right w-20">Skills</Th>
                </tr>
              </thead>
              <tbody>
                {outcomes.map((o, i) => {
                  const tk = o.tokens ?? o.tokens_used ?? 0;
                  const d = o.duration ?? o.duration_s ?? 0;
                  const fm = o.files_modified ?? 0;
                  const fr = o.files_read ?? 0;
                  const sk = o.skills_invoked ?? 0;
                  return (
                    <tr
                      key={o.session_id ?? i}
                      className="h-10 hover:bg-[color:var(--surface-2)] transition-colors border-b border-[color:var(--border-subtle)]"
                    >
                      <td className="px-3 py-1.5">
                        {o.session_id ? (
                          <EntityChip kind="session" label={shortId(o.session_id)} to={`/sessions/${o.session_id}`} title={o.session_id} />
                        ) : (
                          <span className="text-2xs" style={{ color: "var(--text-disabled)" }}>—</span>
                        )}
                      </td>
                      <td className="px-3 py-1.5 font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }} title={o.timestamp ?? o.recorded_at ?? ""}>
                        {fmtTs(o.timestamp ?? o.recorded_at)}
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }}>
                        {d ? `${Math.round(d)}s` : "—"}
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono text-2xs tabular-nums" style={{ color: "var(--text-secondary)" }} title="tokens used">
                        {tk ? fmtTokens(tk) : "—"}
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }} title="files read / modified">
                        {fr}r · {fm}w
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }} title="skills invoked">
                        {sk || "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Skill usage — same table language, restyled from the old divide-y list. */}
      <div>
        <SectionLabel>Skill usage</SectionLabel>
        {skills.length === 0 ? (
          <Empty>No skills invoked yet in scored sessions.</Empty>
        ) : (
          <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr>
                  <Th>Skill</Th>
                  <Th className="text-right w-24">Uses</Th>
                </tr>
              </thead>
              <tbody>
                {skills.map((s) => (
                  <tr
                    key={s.skill}
                    className="h-10 hover:bg-[color:var(--surface-2)] transition-colors border-b border-[color:var(--border-subtle)]"
                  >
                    <td className="px-3 py-1.5 font-mono text-xs" style={{ color: "var(--text-secondary)" }}>{s.skill}</td>
                    <td className="px-3 py-1.5 text-right font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }}>{s.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Page>
  );
}
