import { useEffect, useState, useCallback } from "react";
import { Network, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Kpi, Card, SectionLabel, Empty } from "@/components/ui";
import { cn } from "@/lib/utils";

type Summary = {
  entities: number;
  relationships: number;
  communities: number;
  graph_json_exists: boolean;
  viewer_url: string;
};

type CentralEntity = {
  name: string;
  kind: string;
  file: string;
  line: number | null;
  community: number | null;
  centrality: number;
};

export default function GraphPage() {
  const [project] = useProject();
  const [data, setData] = useState<Summary | null>(null);
  const [central, setCentral] = useState<CentralEntity[] | null>(null);
  const [rebuilding, setRebuilding] = useState(false);

  const load = useCallback(() => {
    api.get<Summary>(`/api/graph/summary?project=${project}`).then(setData).catch(() => setData(null));
    api.get<{ entities: CentralEntity[] }>(`/api/graph/central?project=${project}&limit=15`)
      .then((r) => setCentral(r.entities))
      .catch(() => setCentral([]));
  }, [project]);

  useEffect(load, [load]);

  const rebuild = useCallback(async () => {
    setRebuilding(true);
    try { await api.post(`/api/graph/rebuild?project=${project}`, {}); load(); }
    finally { setRebuilding(false); }
  }, [project, load]);

  return (
    <Page>
      <section className="flex flex-wrap gap-3">
        <Kpi label="Entities" value={data?.entities ?? "—"} />
        <Kpi label="Relationships" value={data?.relationships ?? "—"} />
        <Kpi label="Communities" value={data?.communities ?? "—"} />
        <button
          onClick={rebuild} disabled={rebuilding}
          className="self-stretch min-w-[150px] rounded-md border border-[color:var(--midground-base)]/30 bg-[color:var(--midground-base)]/5 hover:bg-[color:var(--midground-base)]/10 px-4 flex items-center justify-center gap-2 text-sm uppercase tracking-wider disabled:opacity-50 transition-colors"
        >
          <RefreshCw className={cn("w-4 h-4", rebuilding && "animate-spin")} />
          {rebuilding ? "Rebuilding…" : "Rebuild"}
        </button>
      </section>

      <Card className="!p-0 overflow-hidden">
        <div className="px-5 pt-5"><SectionLabel>Code Graph</SectionLabel></div>
        {data?.graph_json_exists ? (
          <iframe
            src={data.viewer_url}
            className="w-full border-0 rounded-b-md"
            style={{ height: "calc(100vh - 280px)", background: "#0f0f1a" }}
          />
        ) : (
          <div className="px-5 pb-5">
            <Empty>
              <Network className="w-8 h-8 mx-auto opacity-30 mb-2" />
              <div>No graph yet for this project — hit Rebuild.</div>
            </Empty>
          </div>
        )}
      </Card>

      <Card className="!p-5">
        <SectionLabel>Top hubs by PageRank</SectionLabel>
        <div className="text-xs opacity-60 mb-3">
          Universal centrality score persisted on every <code>graph_rebuild</code>.
          Drives ranking, RRF boost, and Sigma node size as the Ultimate Graph
          slices land.
        </div>
        {central && central.length > 0 ? (
          <ol className="space-y-1.5 text-sm">
            {central.map((e, i) => (
              <li key={`${e.file}:${e.name}:${i}`} className="flex items-baseline gap-3">
                <span className="opacity-40 font-mono w-6 text-right">{i + 1}.</span>
                <span className="font-mono truncate">{e.name || "(anon)"}</span>
                <span className="opacity-50 text-xs">{e.kind}</span>
                <span className="opacity-40 text-xs truncate flex-1">{e.file}{e.line ? `:${e.line}` : ""}</span>
                <span className="font-mono text-xs tabular-nums opacity-70">
                  {e.centrality.toFixed(4)}
                </span>
              </li>
            ))}
          </ol>
        ) : (
          <Empty>
            <div>No centrality yet — rebuild on v6.1.5+ to compute PageRank.</div>
          </Empty>
        )}
      </Card>
    </Page>
  );
}
