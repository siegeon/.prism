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
  // Ranked matches are the RESULT of the top search bar — they drop down
  // under it on search and close when you pick one (so the canvas fly is
  // visible). No standalone panel cluttering the page.
  const [resultsOpen, setResultsOpen] = useState(false);
  // The clusters currently on the canvas, mirrored up from the viewer's
  // own legend (same enriched names/colors/counts) so the panel below is
  // never out of sync with what's actually in view.
  const [inView, setInView] = useState<{ level: number; levelName: string; items: { label: string; color: string; count: number }[] } | null>(null);

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
        // Open the results dropdown only for an explicit typed search.
        setResultsOpen(q.trim().length > 0 && d.ranked.length > 0);
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
      else if (m?.type === "prism:clusters") setInView({ level: m.level, levelName: m.levelName, items: m.items || [] });
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
  // Closing the dropdown lets the canvas fly + the sticky context show.
  const select = (file: string) => {
    if (!file) return;
    setSelected(file);
    setResultsOpen(false);
    postToViewer([file]);
  };

  return (
    <Page>
      <div className="relative">
        <form onSubmit={submit} className="flex items-stretch gap-2">
          <div className="relative flex-1">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 opacity-40" />
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onFocus={() => { if (data && data.ranked.length > 0) setResultsOpen(true); }}
              onKeyDown={(e) => { if (e.key === "Escape") setResultsOpen(false); }}
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

        {/* Ranked matches = the result of the search bar. Drops down under
            it, floats over the graph, closes when you pick a row. */}
        {resultsOpen && data && data.ranked.length > 0 && (
          <div className="absolute z-30 left-0 right-0 mt-1.5 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] shadow-2xl flex flex-col max-h-[55vh]">
            <div className="flex items-center justify-between px-3 py-2 border-b border-[color:var(--border-default)]">
              <span className="text-[11px] uppercase tracking-wider opacity-70">
                {data.mode === "focus" ? "Ranked matches" : "Top hubs by PageRank"} · {data.ranked.length}
              </span>
              <button onClick={() => setResultsOpen(false)} title="Close" className="opacity-50 hover:opacity-90">
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="overflow-y-auto px-2 py-1.5">
              <RankedList data={data} selected={selected} onSelect={select} />
            </div>
          </div>
        )}
      </div>

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
            style={{ height: "clamp(360px, 52vh, 640px)", background: "#0f0f1a" }}
          />
        ) : (
          <div className="px-5 pb-5 mt-3"><Empty>No graph yet — rebuild on /graph.</Empty></div>
        )}
      </Card>

      {/* Below the graph: clickable values on the LEFT, the result pinned
          on the RIGHT (sticky) so clicking never sends you scrolling. */}
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_1fr] gap-4 items-start">
        <div className="space-y-4 min-w-0">
          {inView && inView.items.length > 0 && (
            <Card className="!p-5">
              <SectionLabel>{inView.levelName} in view · {inView.items.length}</SectionLabel>
              <div className="text-xs opacity-60 mb-2">
                Exactly the clusters on the canvas right now — scroll/drill the graph to change this set.
              </div>
              <div className="flex flex-wrap gap-2">
                {inView.items.map((it, i) => (
                  <span key={it.label + i} title={it.label}
                    className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs border"
                    style={{ borderColor: hexToRgba(it.color, 0.5), background: hexToRgba(it.color, 0.1) }}>
                    <span className="w-2 h-2 rounded-full" style={{ background: it.color }} />
                    {it.label} <span className="opacity-50">· {it.count}</span>
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

const CHIP_CAP = 16;  // chips shown before "+N more" — a wall of 200 is the canvas's job

const ChipNode = ({ n, selected, onSelect }: { n: GNode; selected: string | null; onSelect: (f: string) => void }) => (
  <button onClick={() => onSelect(n.id)} title={n.id}
    className={cn("inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs border max-w-full transition-colors",
      selected === n.id ? "ring-1 ring-[color:var(--text-secondary)]" : "")}
    style={{ borderColor: hexToRgba(commColor(n.community), 0.5), background: hexToRgba(commColor(n.community), 0.08) }}>
    <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: commColor(n.community) }} />
    <span className="truncate font-mono">{n.label}</span>
  </button>
);

// One group of chips (seeds or neighbors), ranked by centrality, capped to
// CHIP_CAP with an expander into a bounded scroll area. Keeps a 200-item
// set skimmable — the hubs first, the rest one click away, the full picture
// on the canvas.
function ChipGroup({ label, total, nodes, selected, onSelect }: {
  label: string; total: number; nodes: GNode[]; selected: string | null; onSelect: (f: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const sorted = useMemo(() => [...nodes].sort((a, b) => (b.centrality ?? 0) - (a.centrality ?? 0)), [nodes]);
  const shown = open ? sorted : sorted.slice(0, CHIP_CAP);
  const hiddenLoaded = sorted.length - shown.length;
  const beyondLoaded = total - sorted.length;  // capped on the backend (canvas has them)
  return (
    <div>
      <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5">
        {label} · {total}{total > nodes.length ? ` (top ${nodes.length} hubs)` : ""}
      </div>
      <div className={cn("flex flex-wrap gap-1.5", open && "max-h-52 overflow-y-auto pr-1")}>
        {shown.map((n) => <ChipNode key={n.id} n={n} selected={selected} onSelect={onSelect} />)}
      </div>
      <div className="mt-1.5 flex items-center gap-3 text-[11px]">
        {hiddenLoaded > 0 && !open && (
          <button onClick={() => setOpen(true)} className="opacity-70 hover:opacity-100 underline-offset-2 hover:underline">
            +{hiddenLoaded} more
          </button>
        )}
        {open && sorted.length > CHIP_CAP && (
          <button onClick={() => setOpen(false)} className="opacity-70 hover:opacity-100 underline-offset-2 hover:underline">
            show fewer
          </button>
        )}
        {beyondLoaded > 0 && (
          <span className="opacity-40">+{beyondLoaded} more in this cluster — explore on the canvas ↑</span>
        )}
      </div>
    </div>
  );
}

// The color-coded structured view of the focused subgraph — the most-central
// seed hubs + their 1-hop neighbors as community-colored chips. The canvas is
// the full visual; this is the ranked, bounded index into it.
function Subgraph({ data, selected, onSelect }: {
  data: Understanding; selected: string | null; onSelect: (f: string) => void;
}) {
  const seeds = data.nodes.filter((n) => n.seed);
  const nbrs = data.nodes.filter((n) => !n.seed);
  const totalSeeds = data.counts.total_seed_files ?? seeds.length;
  return (
    <Card className="!p-5">
      <SectionLabel>Subgraph &amp; relationships</SectionLabel>
      <div className="text-xs opacity-60 mb-3">
        {data.counts.edges} call edges · ranked by centrality, colored by community (same as the canvas) · click a chip to fly there.
      </div>
      <div className="space-y-3">
        <ChipGroup label="Seed hubs" total={totalSeeds} nodes={seeds} selected={selected} onSelect={onSelect} />
        {nbrs.length > 0 && (
          <ChipGroup label="1-hop neighbors" total={nbrs.length} nodes={nbrs} selected={selected} onSelect={onSelect} />
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
