import { useCallback, useEffect, useMemo, useState } from "react";
import { Compass, Network, Search, CornerDownLeft, ArrowRight, ArrowLeft } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Card, Empty, ErrorBanner, Kpi, Page, SectionLabel } from "@/components/ui";
import { ACCENT_HEX, hexToRgba } from "@/lib/palette";
import { cn } from "@/lib/utils";

// --- Ultimate Graph merge (siegeon/.prism#50, slice 4+5) -------------------
// One page that fuses the Brain search lens and the Graph spatial lens.
// Empty query => whole-graph overview (Sigma canvas + top hubs by
// centrality). Typed query => focused subgraph (seed hits + 1-hop
// neighbors) with a ranked list and a per-file context bundle. Backed by
// POST /api/brain/understand, which mirrors the brain_understand MCP tool.

type Ranked = {
  entity_id: string; name: string; kind: string; file: string;
  line?: number | null; community?: number | null; score: number; why: string;
};
type GNode = {
  id: string; label: string; kind: string;
  community?: number | null; centrality?: number; seed?: boolean;
};
type GEdge = { from: string; to: string; weight: number };
type Community = {
  id: number; label: string; size: number; summary: string;
  top_files: string[]; top_entities: string[];
};
type Ctx = {
  entity_id: string; file: string; community?: number | null;
  outline: { name: string; kind: string; line?: number | null }[];
  references: { from: string; weight: number }[];
  call_chain: { to: string; weight: number }[];
  chunks: string[]; annotations: unknown[];
};
type Understanding = {
  query: string; mode: "overview" | "focus";
  nodes: GNode[]; edges: GEdge[]; communities: Community[];
  ranked: Ranked[]; context: Ctx[]; open_questions: unknown[];
  counts: Record<string, number>; provenance: string;
};

const commColor = (id?: number | null) =>
  id == null ? "var(--text-label)" : ACCENT_HEX[id % ACCENT_HEX.length];

const base = (p: string) => (p || "").replace(/\\/g, "/").split("/").pop() || p;

export default function ExplorePage() {
  const [project] = useProject();
  const [input, setInput] = useState("");
  const [data, setData] = useState<Understanding | null>(null);
  const [viewerUrl, setViewerUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null); // selected file

  const run = useCallback((q: string) => {
    setLoading(true); setError(null);
    api.post<Understanding>("/api/brain/understand", { project, query: q, limit: 20, depth: 1 })
      .then((d) => { setData(d); setSelected(d.context[0]?.file ?? null); })
      .catch((e) => { setData(null); setError(String(e?.message || e)); })
      .finally(() => setLoading(false));
  }, [project]);

  useEffect(() => { run(""); }, [run]);
  useEffect(() => {
    api.get<{ graph_json_exists: boolean; viewer_url: string }>(`/api/graph/summary?project=${project}`)
      .then((s) => setViewerUrl(s.graph_json_exists ? s.viewer_url : null))
      .catch(() => setViewerUrl(null));
  }, [project]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    run(input.trim());
  };

  const ctxByFile = useMemo(() => {
    const m = new Map<string, Ctx>();
    (data?.context ?? []).forEach((c) => m.set(c.file, c));
    return m;
  }, [data]);
  const sel = selected ? ctxByFile.get(selected) : undefined;

  return (
    <Page>
      <form onSubmit={submit} className="flex items-stretch gap-2">
        <div className="relative flex-1">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 opacity-40" />
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask the graph — e.g. “community detection”, “reflection loop”. Empty = whole-graph overview."
            className="w-full rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)] pl-9 pr-3 py-2.5 text-sm focus:outline-none focus:border-[color:var(--text-secondary)]"
          />
        </div>
        <button type="submit" disabled={loading}
          className="px-4 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] text-sm uppercase tracking-wider flex items-center gap-2 disabled:opacity-50">
          <CornerDownLeft className="w-4 h-4" /> {loading ? "…" : "Understand"}
        </button>
      </form>

      <section className="flex flex-wrap gap-3">
        <Kpi label="Mode" value={data ? data.mode : "—"} hint={data?.query ? `“${data.query}”` : "overview"} />
        <Kpi label="Nodes" value={data?.counts.nodes ?? "—"} />
        <Kpi label="Edges" value={data?.counts.edges ?? "—"} />
        <Kpi label="Communities" value={data?.counts.communities ?? "—"} />
        <Kpi label="Ranked" value={data?.counts.ranked ?? "—"} />
      </section>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      <div className="grid grid-cols-1 lg:grid-cols-[1.6fr_1fr] gap-4">
        {/* LEFT — the merged lens */}
        <div className="space-y-4 min-w-0">
          <Card className="!p-0 overflow-hidden">
            <div className="px-5 pt-5 flex items-center gap-2">
              <Network className="w-4 h-4 opacity-60" />
              <SectionLabel>{data?.mode === "focus" ? "Focused subgraph" : "Graph — spatial lens"}</SectionLabel>
            </div>
            {data?.mode === "focus"
              ? <Subgraph data={data} selected={selected} onSelect={setSelected} />
              : viewerUrl
                ? <iframe src={viewerUrl} className="w-full border-0 rounded-b-md mt-3"
                    style={{ height: "clamp(360px, 52vh, 620px)", background: "#0f0f1a" }} />
                : <div className="px-5 pb-5 mt-3"><Empty>No graph yet — rebuild on /graph.</Empty></div>}
          </Card>

          <Card className="!p-5">
            <SectionLabel>{data?.mode === "focus" ? "Ranked matches" : "Top hubs by PageRank"}</SectionLabel>
            <div className="text-xs opacity-60 mb-3">
              {data?.mode === "focus"
                ? "Brain hybrid search (BM25 + vector + graph) — the rank/focus lens onto the same store."
                : "Universal centrality score, the default rank when there is no query."}
            </div>
            <RankedList data={data} selected={selected} onSelect={setSelected} />
          </Card>

          {(data?.communities?.length ?? 0) > 0 && (
            <Card className="!p-5">
              <SectionLabel>Communities {data?.mode === "focus" ? "in view" : ""}</SectionLabel>
              <div className="flex flex-wrap gap-2 mt-2">
                {data!.communities.map((c) => (
                  <span key={c.id} title={c.summary}
                    className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs border"
                    style={{ borderColor: hexToRgba(commColor(c.id), 0.5), background: hexToRgba(commColor(c.id), 0.1) }}>
                    <span className="w-2 h-2 rounded-full" style={{ background: commColor(c.id) }} />
                    {c.label} <span className="opacity-50">· {c.size}</span>
                  </span>
                ))}
              </div>
            </Card>
          )}
        </div>

        {/* RIGHT — context bundle */}
        <ContextRail data={data} sel={sel} selected={selected} />
      </div>
    </Page>
  );
}

function RankedList({ data, selected, onSelect }: {
  data: Understanding | null; selected: string | null; onSelect: (f: string) => void;
}) {
  if (!data || data.ranked.length === 0) return <Empty>No results.</Empty>;
  return (
    <ol className="space-y-1">
      {data.ranked.map((r, i) => (
        <li key={r.entity_id + i}>
          <button onClick={() => r.file && onSelect(r.file)}
            className={cn("w-full text-left flex items-baseline gap-3 px-2 py-1.5 rounded-md hover:bg-[color:var(--surface-2)] transition-colors",
              selected && r.file === selected && "bg-[color:var(--surface-2)] ring-1 ring-[color:var(--border-default)]")}>
            <span className="opacity-40 font-mono w-6 text-right text-xs shrink-0">{i + 1}.</span>
            <span className="font-mono text-sm truncate">{r.name || "(anon)"}</span>
            {r.kind && <span className="opacity-50 text-xs shrink-0">{r.kind}</span>}
            <span className="opacity-40 text-xs truncate flex-1">{base(r.file)}{r.line ? `:${r.line}` : ""}</span>
            <span className="font-mono text-xs tabular-nums opacity-70 shrink-0">{r.score.toFixed(4)}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}

function Subgraph({ data, selected, onSelect }: {
  data: Understanding; selected: string | null; onSelect: (f: string) => void;
}) {
  const seeds = data.nodes.filter((n) => n.seed);
  const nbrs = data.nodes.filter((n) => !n.seed);
  const Node = (n: GNode) => (
    <button key={n.id} onClick={() => onSelect(n.id)}
      title={n.id}
      className={cn("inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs border max-w-full",
        selected === n.id ? "ring-1 ring-[color:var(--text-secondary)]" : "")}
      style={{ borderColor: hexToRgba(commColor(n.community), 0.5), background: hexToRgba(commColor(n.community), 0.08) }}>
      <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: commColor(n.community) }} />
      <span className="truncate font-mono">{n.label}</span>
    </button>
  );
  return (
    <div className="px-5 pb-5 mt-3 space-y-3">
      <div>
        <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5">
          Seeds · {seeds.length}
        </div>
        <div className="flex flex-wrap gap-1.5">{seeds.map(Node)}</div>
      </div>
      {nbrs.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5">
            1-hop neighbors · {nbrs.length}
          </div>
          <div className="flex flex-wrap gap-1.5 opacity-80">{nbrs.map(Node)}</div>
        </div>
      )}
      {data.edges.length > 0 && (
        <div className="text-xs opacity-50">
          {data.edges.length} call edges between these files · click a node for its context →
        </div>
      )}
    </div>
  );
}

function ContextRail({ data, sel, selected }: {
  data: Understanding | null; sel?: Ctx; selected: string | null;
}) {
  return (
    <Card className="!p-5 lg:sticky lg:top-4 self-start max-h-[calc(100vh-2rem)] overflow-y-auto">
      <div className="flex items-center gap-2">
        <Compass className="w-4 h-4 opacity-60" />
        <SectionLabel>Context bundle</SectionLabel>
      </div>
      {!selected ? (
        <Empty>{data?.mode === "overview"
          ? "Type a query, then pick a result to see its outline, callers and callees."
          : "Pick a node or ranked match."}</Empty>
      ) : !sel ? (
        <div className="mt-3 text-sm">
          <div className="font-mono break-all text-xs opacity-70 mb-1">{selected}</div>
          <div className="opacity-50 text-xs">No bundle for this node — it is a 1-hop neighbor, not a seed hit.</div>
        </div>
      ) : (
        <div className="mt-3 space-y-4">
          <div className="font-mono break-all text-xs opacity-70">{sel.file}</div>

          {sel.chunks.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5">Matched</div>
              {sel.chunks.map((c, i) => (
                <p key={i} className="text-xs leading-relaxed opacity-80 border-l-2 border-[color:var(--border-default)] pl-2 mb-1.5">{c}…</p>
              ))}
            </div>
          )}

          <Section label={`Outline · ${sel.outline.length}`}>
            {sel.outline.length === 0 ? <Faint>none</Faint> : (
              <ul className="space-y-0.5">
                {sel.outline.map((o, i) => (
                  <li key={i} className="flex items-baseline gap-2 text-xs">
                    <span className="font-mono truncate">{o.name}</span>
                    <span className="opacity-40">{o.kind}{o.line ? ` :${o.line}` : ""}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section label={`Callers · ${sel.references.length}`} icon={<ArrowLeft className="w-3 h-3" />}>
            {sel.references.length === 0 ? <Faint>none</Faint> :
              sel.references.map((r, i) => <Edge key={i} file={r.from} w={r.weight} />)}
          </Section>

          <Section label={`Callees · ${sel.call_chain.length}`} icon={<ArrowRight className="w-3 h-3" />}>
            {sel.call_chain.length === 0 ? <Faint>none</Faint> :
              sel.call_chain.map((r, i) => <Edge key={i} file={r.to} w={r.weight} />)}
          </Section>

          <div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5">Narrative</div>
            <div className="text-xs opacity-50 italic border border-dashed border-[color:var(--border-default)] rounded-md px-2 py-1.5">
              LLM annotations land here once the background enrichment loop ships
              (epic slices 1–2, 6). Structure above is deterministic.
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

const Section = ({ label, icon, children }: { label: string; icon?: React.ReactNode; children: React.ReactNode }) => (
  <div>
    <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5 flex items-center gap-1">
      {icon}{label}
    </div>
    {children}
  </div>
);
const Faint = ({ children }: { children: React.ReactNode }) => <div className="text-xs opacity-40">{children}</div>;
const Edge = ({ file, w }: { file: string; w: number }) => (
  <div className="flex items-baseline gap-2 text-xs">
    <span className="font-mono truncate flex-1" title={file}>{base(file)}</span>
    <span className="opacity-40 tabular-nums">×{w}</span>
  </div>
);
