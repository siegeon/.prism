import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Compass, Network, Search, CornerDownLeft, ArrowRight, ArrowLeft, X, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Card, Empty, ErrorBanner, Pill, SectionLabel, toneFromLabel } from "@/components/ui";
import { communityColor, hexToRgba } from "@/lib/palette";
import { cn } from "@/lib/utils";

// The brain holds more than code: docs, comments, expertise/domain notes.
// The domain filter slices the search to those — "expertise"/"md" reach the
// unstructured knowledge the graph canvas alone doesn't show.
const DOMAINS = ["all", "py", "ts", "md", "expertise"];

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
// A cluster in the canvas legend, mirrored into the panel. Carries the
// viewer's node id so clicking the panel chip can drill the canvas to it.
type ClusterItem = { label: string; color: string; count: number; kind?: string; id?: string; cid?: number };
type Annotation = {
  scope_kind: "node" | "community" | "hierarchy";
  scope_id: string;
  name: string; purpose: string;
  provenance: string;       // "deterministic" | "claude @ <date>"
  updated_at?: string | null;
};
type Ctx = {
  entity_id: string; file: string; community?: number | null;
  outline: { name: string; kind: string; line?: number | null }[];
  references: { from: string; weight: number }[];
  call_chain: { to: string; weight: number }[];
  chunks: string[]; annotations: Annotation[];
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
  const [inView, setInView] = useState<{ level: number; levelName: string; items: ClusterItem[] } | null>(null);
  const [domain, setDomain] = useState("all");
  const [busy, setBusy] = useState<string | null>(null);  // which control is running

  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const filesRef = useRef<string[]>([]);
  // Symbol carried by a /brain?focus=<file>&symbol=<name> deep-link, so the
  // viewer can light the precise symbol node (not the whole file). Held in a
  // ref so the viewer-ready re-post re-applies it once the canvas is built.
  const symbolRef = useRef<string | null>(null);

  const postToViewer = useCallback((files: string[], symbol?: string | null) => {
    const win = iframeRef.current?.contentWindow;
    if (!win) return;
    win.postMessage(
      files.length ? { type: "prism:search", files, symbol: symbol ?? null } : { type: "prism:clear" },
      "*");
  }, []);

  const run = useCallback((q: string, dom: string = domain) => {
    setLoading(true); setError(null);
    api.post<Understanding>("/api/brain/understand", { project, query: q, limit: 20, depth: 1, domain: dom === "all" ? null : dom })
      .then((d) => {
        setData(d);
        setSelected(d.context[0]?.file ?? null);
        // Open the results dropdown only for an explicit typed search.
        setResultsOpen(q.trim().length > 0 && d.ranked.length > 0);
        const files = d.mode === "focus" ? d.nodes.map((n) => n.id) : [];
        filesRef.current = files;
        symbolRef.current = null;  // a typed search supersedes any deep-link symbol
        postToViewer(files);
      })
      .catch((e) => { setData(null); setError(String(e?.message || e)); })
      .finally(() => setLoading(false));
  }, [project, postToViewer, domain]);

  // Reindex Brain / Rebuild graph / Enrich clusters — the maintenance
  // actions folded in from the old Brain + Graph pages, kept compact.
  const runControl = useCallback((which: string, url: string) => {
    setBusy(which); setError(null);
    api.post(url, {})
      .then(() => run(input.trim()))
      .catch((e) => setError(String(e?.message || e)))
      .finally(() => setBusy(null));
  }, [run, input]);

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

  // Deep-link entry hop (/brain?focus=<file>&symbol=<name>, symbol optional).
  // Reuses the page's existing focus machinery: understand on the seed file,
  // select it so ContextRail opens, and steer the canvas via postMessage --
  // symbol-precise when a symbol is given. Same path as a canvas click, just
  // driven from the URL instead of a gesture.
  const focusSeed = useCallback((file: string, symbol: string | null) => {
    if (!file) return;
    setLoading(true); setError(null);
    setInput(symbol || base(file));
    api.post<Understanding>("/api/brain/understand", { project, seed_files: [file], label: symbol || base(file), limit: 20, depth: 1 })
      .then((d) => {
        setData(d);
        const seedFile = d.context[0]?.file ?? file;
        setSelected(seedFile);
        filesRef.current = [seedFile];
        symbolRef.current = symbol;
        postToViewer([seedFile], symbol);
      })
      .catch((e) => setError(String(e?.message || e)))
      .finally(() => setLoading(false));
  }, [project, postToViewer]);

  // Read the deep-link once on mount. Present -> focus that seed; absent ->
  // the usual whole-graph overview.
  const deepLink = useMemo(() => {
    const p = new URLSearchParams(window.location.search);
    const file = p.get("focus");
    return file ? { file, symbol: p.get("symbol") } : null;
  }, []);

  useEffect(() => { if (!deepLink) run(""); }, [run, deepLink]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (deepLink) focusSeed(deepLink.file, deepLink.symbol); }, []);
  useEffect(() => {
    api.get<{ graph_json_exists: boolean; viewer_url: string }>(`/api/graph/summary?project=${project}`)
      .then((s) => setViewerUrl(s.graph_json_exists ? s.viewer_url : null))
      .catch(() => setViewerUrl(null));
  }, [project]);

  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const m = e.data;
      if (m?.type === "prism:viewer-ready") postToViewer(filesRef.current, symbolRef.current);
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
    symbolRef.current = null;  // manual pick supersedes any deep-link symbol
    postToViewer([file]);
  };

  // Quick-filter from a "domains in view" chip: drill the canvas to that
  // cluster (top of its domain, siblings dimmed). The viewer drills + posts
  // its members back up, which refills the panels.
  const drillCluster = (it: ClusterItem) => {
    iframeRef.current?.contentWindow?.postMessage(
      { type: "prism:drill", kind: it.kind, id: it.id, cid: it.cid, label: it.label }, "*");
  };

  return (
    <div className="h-full flex flex-col gap-3 px-5 py-4 min-w-[720px]">
      <div className="relative shrink-0">
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
              <span className="text-2xs uppercase tracking-wider opacity-70">
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

      {/* Domain filter (reach the brain's unstructured knowledge: docs,
          comments, expertise) + maintenance controls folded from the old
          Brain/Graph pages. */}
      <div className="flex flex-wrap items-center gap-2 shrink-0">
        <span className="text-2xs uppercase tracking-wider opacity-40 mr-1">domain</span>
        {DOMAINS.map((d) => (
          <Pill key={d} active={domain === d} tone={toneFromLabel(d)}
            onClick={() => { setDomain(d); run(input.trim(), d); }}>{d}</Pill>
        ))}
        <div className="ml-auto flex items-center gap-1.5">
          <Ctl label="Reindex" running={busy === "reindex"} onClick={() => runControl("reindex", `/api/brain/reindex?project=${project}`)} />
          <Ctl label="Rebuild" running={busy === "rebuild"} onClick={() => runControl("rebuild", `/api/graph/rebuild?project=${project}`)} />
          <Ctl label="Enrich" running={busy === "enrich"} onClick={() => runControl("enrich", `/api/graph/enrich?project=${project}`)} />
        </div>
      </div>

      {/* Compact stat strip — one line, no big boxes hogging vertical space */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs shrink-0">
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

      {error && <div className="shrink-0"><ErrorBanner>{error}</ErrorBanner></div>}

      {/* CENTERPIECE — the WebGL canvas fills the available height so the
          page fits one screen; the panels sit in a bounded strip below. */}
      <Card className="!p-0 overflow-hidden flex-1 min-h-0 flex flex-col">
        <div className="px-5 pt-3 pb-2 flex items-center gap-2 shrink-0">
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
            className="w-full flex-1 min-h-0 border-0 rounded-b-md"
            style={{ background: "#0f0f1a" }}
          />
        ) : (
          <div className="px-5 pb-5"><Empty>No graph yet — rebuild on /graph.</Empty></div>
        )}
      </Card>

      {/* Panels in a bounded strip that scrolls internally — keeps the
          whole page on one screen. Clickable values left, context right. */}
      <div className="shrink-0 overflow-y-auto grid grid-cols-1 lg:grid-cols-[1fr_1fr] gap-4 items-start"
           style={{ maxHeight: "32vh" }}>
        <div className="space-y-4 min-w-0">
          {inView && inView.items.length > 0 && (
            <Card className="!p-5">
              <SectionLabel>{inView.levelName} in view · {inView.items.length}</SectionLabel>
              <div className="text-xs opacity-60 mb-2">
                Click a cluster to drill the graph to it — top of its domain, the rest dimmed.
              </div>
              <div className="flex flex-wrap gap-2">
                {inView.items.map((it, i) => (
                  <button key={it.label + i} title={`Drill into ${it.label}`}
                    onClick={() => drillCluster(it)}
                    className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs border transition-colors hover:brightness-125"
                    style={{ borderColor: hexToRgba(it.color, 0.5), background: hexToRgba(it.color, 0.1) }}>
                    <span className="w-2 h-2 rounded-full" style={{ background: it.color }} />
                    {it.label} <span className="opacity-50">· {it.count}</span>
                  </button>
                ))}
              </div>
            </Card>
          )}

          {data?.mode === "focus" && data.nodes.length > 0 && (
            <Subgraph data={data} selected={selected} onSelect={select} />
          )}
        </div>

        {/* RESULT — context for whatever you click on the left */}
        <div className="min-w-0">
          <ContextRail sel={sel} selected={selected} mode={data?.mode} />
        </div>
      </div>
    </div>
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
      <div className="text-2xs uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5">
        {label} · {total}{total > nodes.length ? ` (top ${nodes.length} hubs)` : ""}
      </div>
      <div className={cn("flex flex-wrap gap-1.5", open && "max-h-52 overflow-y-auto pr-1")}>
        {shown.map((n) => <ChipNode key={n.id} n={n} selected={selected} onSelect={onSelect} />)}
      </div>
      <div className="mt-1.5 flex items-center gap-3 text-2xs">
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
          <Section label={`Narrative · ${sel.annotations.length}`}>
            {sel.annotations.length === 0 ? (
              <Faint>no annotations yet — structure above is deterministic</Faint>
            ) : (
              <ul className="space-y-2">
                {sel.annotations.map((a, i) => {
                  const isLlm = a.provenance.startsWith("claude @");
                  return (
                    <li key={i} className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] px-2.5 py-2">
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <span className="text-xs font-medium text-[color:var(--text-primary)] truncate">{a.name}</span>
                        <Pill tone={isLlm ? "violet" : "slate"} active>
                          {isLlm ? a.provenance : "deterministic"}
                        </Pill>
                      </div>
                      <p className="text-xs leading-relaxed opacity-80">{a.purpose}</p>
                      <div className="mt-1 flex items-center gap-2 text-2xs uppercase tracking-wider opacity-40">
                        <span>{a.scope_kind}</span>
                        {a.updated_at ? <span>{a.updated_at.slice(0, 10)}</span> : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </Section>
        </div>
      )}
    </Card>
  );
}

const Ctl = ({ label, running, onClick }: { label: string; running: boolean; onClick: () => void }) => (
  <button onClick={onClick} disabled={running}
    className="inline-flex items-center gap-1 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] px-2 py-1 text-2xs uppercase tracking-wider disabled:opacity-50">
    <RefreshCw className={cn("w-3 h-3", running && "animate-spin")} />
    {running ? "…" : label}
  </button>
);
const Stat = ({ label, v }: { label: string; v?: number }) => (
  <span className="flex items-baseline gap-1">
    <span className="font-mono tabular-nums text-[color:var(--text-primary)]">{v ?? "—"}</span>
    <span className="opacity-50 uppercase tracking-wider text-2xs">{label}</span>
  </span>
);
const Label = ({ children }: { children: React.ReactNode }) => (
  <div className="text-2xs uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5">{children}</div>
);
const Section = ({ label, icon, children }: { label: string; icon?: React.ReactNode; children: React.ReactNode }) => (
  <div>
    <div className="text-2xs uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5 flex items-center gap-1">{icon}{label}</div>
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
