import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Compass, Network, Search, CornerDownLeft, ArrowRight, ArrowLeft, X } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Card, Empty, ErrorBanner, Page, SectionLabel } from "@/components/ui";
import { communityColor, hexToRgba } from "@/lib/palette";
import { cn } from "@/lib/utils";

// --- Ultimate Graph merge (siegeon/.prism#50, slice 4+5) -------------------
// One page, ONE WebGL canvas as the centerpiece (the tuned, animated,
// community-colored Sigma viewer). Search STEERS it via postMessage. Below
// the canvas, the same result is laid out in our color-coded structured way:
// the communities legend + the seed/neighbor subgraph as community-colored
// node chips + the per-node relationships. Ranked list + context bundle ride
// in the rail. Backed by POST /api/brain/understand (= brain_understand MCP).

type Ranked = {
  entity_id: string; name: string; kind: string; file: string;
  line?: number | null; community?: number | null; score: number; why: string;
};
type GNode = { id: string; label: string; kind: string; community?: number | null; centrality?: number; seed?: boolean };
type GEdge = { from: string; to: string; weight: number };
type Community = { id: number; label: string; size: number; summary: string; top_files: string[]; top_entities: string[] };
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
  ranked: Ranked[]; context: Ctx[];
  counts: Record<string, number>; provenance: string;
};

const base = (p: string) => (p || "").replace(/\\/g, "/").split("/").pop() || p;
// communityColor() is the single shared domain (palette.ts) used by the
// WebGL canvas, its Clusters legend, and these panels — same id, same hue.
const commColor = communityColor;

export default function ExplorePage() {
  const [project] = useProject();
  const [input, setInput] = useState("");
  const [data, setData] = useState<Understanding | null>(null);
  const [viewerUrl, setViewerUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const filesRef = useRef<string[]>([]);

  const postToViewer = useCallback((files: string[]) => {
    const win = iframeRef.current?.contentWindow;
    if (!win) return;
    win.postMessage(files.length ? { type: "prism:search", files } : { type: "prism:clear" }, "*");
  }, []);

  const run = useCallback((q: string) => {
    setLoading(true); setError(null);
    api.post<Understanding>("/api/brain/understand", { project, query: q, limit: 20, depth: 1 })
      .then((d) => {
        setData(d);
        setSelected(d.context[0]?.file ?? null);
        const files = d.mode === "focus" ? d.nodes.map((n) => n.id) : [];
        filesRef.current = files;
        postToViewer(files);
      })
      .catch((e) => { setData(null); setError(String(e?.message || e)); })
      .finally(() => setLoading(false));
  }, [project, postToViewer]);

  // Click-through FROM the canvas: a cluster / super-node / node was
  // clicked in the viewer; load the full Understand payload for its files.
  // We don't re-steer the canvas here — the viewer already drilled/focused
  // on the click — we just fill the ranked + context + subgraph panels.
  const loadSelection = useCallback((label: string, files: string[]) => {
    if (!files.length) return;
    setLoading(true); setError(null);
    setInput(label);
    api.post<Understanding>("/api/brain/understand", { project, seed_files: files, label, limit: 20, depth: 1 })
      .then((d) => { setData(d); setSelected(d.context[0]?.file ?? null); })
      .catch((e) => setError(String(e?.message || e)))
      .finally(() => setLoading(false));
  }, [project]);

  useEffect(() => { run(""); }, [run]);
  useEffect(() => {
    api.get<{ graph_json_exists: boolean; viewer_url: string }>(`/api/graph/summary?project=${project}`)
      .then((s) => setViewerUrl(s.graph_json_exists ? s.viewer_url : null))
      .catch(() => setViewerUrl(null));
  }, [project]);

  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const m = e.data;
      if (m?.type === "prism:viewer-ready") postToViewer(filesRef.current);
      else if (m?.type === "prism:explore") loadSelection(m.label || "selection", m.files || []);
    };
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, [postToViewer, loadSelection]);

  const submit = (e: React.FormEvent) => { e.preventDefault(); run(input.trim()); };
  const clear = () => { setInput(""); run(""); };

  const ctxByFile = useMemo(() => {
    const m = new Map<string, Ctx>();
    (data?.context ?? []).forEach((c) => m.set(c.file, c));
    return m;
  }, [data]);
  const sel = selected ? ctxByFile.get(selected) : undefined;

  // Selecting a file opens its context bundle AND flies the canvas to it.
  const select = (file: string) => {
    if (!file) return;
    setSelected(file);
    postToViewer([file]);
  };

  return (
    <Page>
      <form onSubmit={submit} className="flex items-stretch gap-2">
        <div className="relative flex-1">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 opacity-40" />
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask the graph — the canvas flies to your matches. Empty = whole-graph overview."
            className="w-full rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)] pl-9 pr-9 py-2.5 text-sm focus:outline-none focus:border-[color:var(--text-secondary)]"
          />
          {input && (
            <button type="button" onClick={clear} title="Clear"
              className="absolute right-2 top-1/2 -translate-y-1/2 opacity-40 hover:opacity-90">
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
        <button type="submit" disabled={loading}
          className="px-4 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] text-sm uppercase tracking-wider flex items-center gap-2 disabled:opacity-50">
          <CornerDownLeft className="w-4 h-4" /> {loading ? "…" : "Understand"}
        </button>
      </form>

      {/* Compact stat strip — one line, no big boxes hogging vertical space */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <span className="uppercase tracking-wider px-2 py-0.5 rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)]">
          {data ? data.mode : "—"}
        </span>
        {data?.query && <span className="opacity-60 truncate max-w-[40ch]">“{data.query}”</span>}
        <Stat label="nodes" v={data?.counts.nodes} />
        <Stat label="edges" v={data?.counts.edges} />
        <Stat label="communities" v={data?.counts.communities} />
        <Stat label="ranked" v={data?.counts.ranked} />
        <span className="opacity-40 ml-auto">
          {data?.mode === "focus" ? "click empty canvas to clear focus" : "type a query to focus the graph"}
        </span>
      </div>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      {/* CENTERPIECE — the one WebGL canvas, full width, steered by search */}
      <Card className="!p-0 overflow-hidden">
        <div className="px-5 pt-5 flex items-center gap-2">
          <Network className="w-4 h-4 opacity-60" />
          <SectionLabel>Graph</SectionLabel>
          <span className="text-xs opacity-50 ml-1">
            {data?.mode === "focus"
              ? "highlighting your matches — scroll to explore, click empty space to clear"
              : "whole graph, colored by community — type a query to focus it"}
          </span>
        </div>
        {viewerUrl ? (
          <iframe
            ref={iframeRef}
            src={viewerUrl}
            className="w-full border-0 rounded-b-md mt-3"
            style={{ height: "clamp(520px, 70vh, 900px)", background: "#0f0f1a" }}
          />
        ) : (
          <div className="px-5 pb-5 mt-3"><Empty>No graph yet — rebuild on /graph.</Empty></div>
        )}
      </Card>

      {/* Below the graph: clickable values on the LEFT, the result pinned
          on the RIGHT (sticky) so clicking never sends you scrolling. */}
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_1fr] gap-4 items-start">
        <div className="space-y-4 min-w-0">
          <Card className="!p-5">
            <SectionLabel>{data?.mode === "focus" ? "Ranked matches" : "Top hubs by PageRank"}</SectionLabel>
            <div className="text-xs opacity-60 mb-3">
              {data?.mode === "focus"
                ? "Brain hybrid search — click a row to fly the canvas + see context →"
                : "Centrality rank — click to focus the canvas + see context →"}
            </div>
            <RankedList data={data} selected={selected} onSelect={select} />
          </Card>

          {(data?.communities?.length ?? 0) > 0 && (
            <Card className="!p-5">
              <SectionLabel>{data?.mode === "focus" ? "Communities in view" : "Communities"}</SectionLabel>
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

          {data?.mode === "focus" && data.nodes.length > 0 && (
            <Subgraph data={data} selected={selected} onSelect={select} />
          )}
        </div>

        {/* RESULT — pinned in view while you click around on the left */}
        <div className="lg:sticky lg:top-4 min-w-0">
          <ContextRail sel={sel} selected={selected} mode={data?.mode} />
        </div>
      </div>
    </Page>
  );
}

function RankedList({ data, selected, onSelect }: {
  data: Understanding | null; selected: string | null; onSelect: (f: string) => void;
}) {
  if (!data || data.ranked.length === 0) return <Empty>No results.</Empty>;
  return (
    <ol className="space-y-1 max-h-[44vh] overflow-y-auto">
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

// The color-coded structured view of the focused subgraph — seed hits and
// their 1-hop neighbors as community-colored chips, plus the relationship
// count. Mirrors the canvas's coloring so the two read as one thing.
function Subgraph({ data, selected, onSelect }: {
  data: Understanding; selected: string | null; onSelect: (f: string) => void;
}) {
  const seeds = data.nodes.filter((n) => n.seed);
  const nbrs = data.nodes.filter((n) => !n.seed);
  const Node = (n: GNode) => (
    <button key={n.id} onClick={() => onSelect(n.id)} title={n.id}
      className={cn("inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs border max-w-full transition-colors",
        selected === n.id ? "ring-1 ring-[color:var(--text-secondary)]" : "")}
      style={{ borderColor: hexToRgba(commColor(n.community) as string, 0.5), background: hexToRgba(commColor(n.community) as string, 0.08) }}>
      <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: commColor(n.community) }} />
      <span className="truncate font-mono">{n.label}</span>
    </button>
  );
  return (
    <Card className="!p-5">
      <SectionLabel>Subgraph &amp; relationships</SectionLabel>
      <div className="text-xs opacity-60 mb-3">
        {data.counts.edges} call edges across {data.nodes.length} files · colored by community, same as the canvas · click a chip to fly there.
      </div>
      <div className="space-y-3">
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5">Seeds · {seeds.length}</div>
          <div className="flex flex-wrap gap-1.5">{seeds.map(Node)}</div>
        </div>
        {nbrs.length > 0 && (
          <div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5">1-hop neighbors · {nbrs.length}</div>
            <div className="flex flex-wrap gap-1.5 opacity-90">{nbrs.map(Node)}</div>
          </div>
        )}
      </div>
    </Card>
  );
}

function ContextRail({ sel, selected, mode }: { sel?: Ctx; selected: string | null; mode?: string }) {
  return (
    <Card className="!p-5">
      <div className="flex items-center gap-2">
        <Compass className="w-4 h-4 opacity-60" />
        <SectionLabel>Context bundle</SectionLabel>
      </div>
      {!selected ? (
        <Empty>{mode === "overview"
          ? "Type a query, then pick a result to see its outline, callers and callees."
          : "Pick a ranked match."}</Empty>
      ) : !sel ? (
        <div className="mt-3 text-sm">
          <div className="font-mono break-all text-xs opacity-70 mb-1">{selected}</div>
          <div className="opacity-50 text-xs">No bundle for this file — it's a 1-hop neighbor, not a seed hit.</div>
        </div>
      ) : (
        <div className="mt-3 space-y-4">
          <div className="font-mono break-all text-xs opacity-70">{sel.file}</div>
          {sel.chunks.length > 0 && (
            <div>
              <Label>Matched</Label>
              {sel.chunks.map((c, i) => (
                <p key={i} className="text-xs leading-relaxed opacity-80 border-l-2 border-[color:var(--border-default)] pl-2 mb-1.5">{c}…</p>
              ))}
            </div>
          )}
          <Section label={`Outline · ${sel.outline.length}`}>
            {sel.outline.length === 0 ? <Faint>none</Faint> : (
              <ul className="space-y-0.5 max-h-44 overflow-y-auto">
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
            <Label>Narrative</Label>
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

const Stat = ({ label, v }: { label: string; v?: number }) => (
  <span className="flex items-baseline gap-1">
    <span className="font-mono tabular-nums text-[color:var(--text-primary)]">{v ?? "—"}</span>
    <span className="opacity-50 uppercase tracking-wider text-[10px]">{label}</span>
  </span>
);
const Label = ({ children }: { children: React.ReactNode }) => (
  <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5">{children}</div>
);
const Section = ({ label, icon, children }: { label: string; icon?: React.ReactNode; children: React.ReactNode }) => (
  <div>
    <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5 flex items-center gap-1">{icon}{label}</div>
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
