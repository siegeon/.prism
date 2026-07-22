import { useEffect, useState, useCallback } from "react";
import { ThumbsUp, ThumbsDown } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, SectionLabel, Empty } from "@/components/ui";
import { Lozenge } from "@/components/Lozenge";
import { EntityChip } from "@/components/EntityChip";
import XrefLink from "@/components/XrefLink";

type Hit = {
  doc_id: string;
  rrf_score?: number | null;
  rerank_score?: number | null;
  domain?: string | null;
  entity_name?: string | null;
};

type SearchRow = {
  id: number;
  ts?: string;
  query: string;
  mode?: string;
  rerank?: string;
  latency_ms?: number;
  n_results?: number;
  final_top?: string; // JSON-encoded Hit[]
  // Attribution: WHO asked (added to the /api/retrievals payload). Absent on
  // legacy rows logged before the searches table gained these columns.
  session_id?: string | null;
  task_id?: string | null;
};

function parseHits(raw: string | undefined): Hit[] {
  if (!raw) return [];
  try { return JSON.parse(raw) as Hit[]; } catch { return []; }
}

const shortId = (id?: string | null) => (id ?? "").slice(0, 8);

export default function RetrievalsPage() {
  const [project] = useProject();
  const [rows, setRows] = useState<SearchRow[]>([]);

  const load = useCallback(() => {
    api.get<{ searches: SearchRow[] }>(`/api/retrievals?project=${project}&limit=50`)
      .then((d) => setRows(d.searches))
      .catch(() => setRows([]));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  const rate = useCallback((search_id: number, doc_id: string, signal: "up" | "down") => {
    const params = new URLSearchParams({
      search_id: String(search_id), doc_id, signal, project,
    });
    api.post(`/api/retrievals/feedback?${params}`, {}).then(load);
  }, [project, load]);

  return (
    <Page>
      <div>
        <SectionLabel>Recent retrievals</SectionLabel>
        {rows.length === 0 ? (
          <Empty>No retrievals recorded yet.</Empty>
        ) : (
          <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr>
                  <th className="text-left text-2xs uppercase tracking-wider font-semibold px-3 py-2 border-b border-[color:var(--border-default)]" style={{ color: "var(--text-muted)" }}>Query</th>
                  <th className="text-left text-2xs uppercase tracking-wider font-semibold px-3 py-2 w-32 border-b border-[color:var(--border-default)]" style={{ color: "var(--text-muted)" }}>Mode</th>
                  <th className="text-right text-2xs uppercase tracking-wider font-semibold px-3 py-2 w-16 border-b border-[color:var(--border-default)]" style={{ color: "var(--text-muted)" }}>Hits</th>
                  <th className="text-right text-2xs uppercase tracking-wider font-semibold px-3 py-2 w-20 border-b border-[color:var(--border-default)]" style={{ color: "var(--text-muted)" }}>Latency</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const hits = parseHits(r.final_top);
                  return (
                    <tr key={r.id} className="hover:bg-[color:var(--surface-2)] transition-colors border-b border-[color:var(--border-subtle)] align-top">
                      <td className="px-3 py-2 min-w-0">
                        <div className="font-medium truncate" style={{ color: "var(--text-primary)" }} title={r.query}>{r.query}</div>
                        {/* Attribution + timestamp: session / task as chips, no longer bare underlined links. */}
                        {(r.ts || r.session_id || r.task_id) && (
                          <div className="flex flex-wrap items-center gap-2 mt-1.5">
                            {r.ts && <span className="text-2xs font-mono tabular-nums" style={{ color: "var(--text-muted)" }}>{r.ts}</span>}
                            {r.session_id && <EntityChip kind="session" label={shortId(r.session_id)} to={`/sessions/${r.session_id}`} title={`asking session ${r.session_id}`} />}
                            {r.task_id && <EntityChip kind="task" label={shortId(r.task_id)} to={`/tasks/${r.task_id}`} title={`asking task ${r.task_id}`} />}
                          </div>
                        )}
                        {/* Doc hits keep XrefLink; thumbs feedback stays wired. */}
                        {hits.length > 0 && (
                          <div className="mt-2 space-y-1">
                            {hits.slice(0, 3).map((h, i) => (
                              <div key={`${r.id}-${i}-${h.doc_id}`} className="flex items-center gap-3 text-xs">
                                <span className="flex-1 min-w-0 truncate"><XrefLink token={h.doc_id} /></span>
                                <span className="font-mono text-2xs tabular-nums w-12 text-right" style={{ color: "var(--text-muted)" }}>{h.rrf_score?.toFixed?.(3) ?? ""}</span>
                                <button onClick={() => rate(r.id, h.doc_id, "up")} className="p-1 rounded hover:bg-[color:var(--surface-2)]" title="Mark as useful"><ThumbsUp className="w-3 h-3" /></button>
                                <button onClick={() => rate(r.id, h.doc_id, "down")} className="p-1 rounded hover:bg-[color:var(--surface-2)]" title="Mark as not useful"><ThumbsDown className="w-3 h-3" /></button>
                              </div>
                            ))}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex flex-wrap items-center gap-1.5">
                          {r.mode && <Lozenge tone="neutral">{r.mode}</Lozenge>}
                          {r.rerank && r.rerank !== "off" && <Lozenge tone="info">rerank</Lozenge>}
                        </div>
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-2xs tabular-nums" style={{ color: "var(--text-secondary)" }}>{r.n_results ?? 0}</td>
                      <td className="px-3 py-2 text-right font-mono text-2xs tabular-nums" style={{ color: "var(--text-muted)" }}>{r.latency_ms ?? 0}ms</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Page>
  );
}
