import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Compass, Network, Search, CornerDownLeft, ArrowRight, ArrowLeft, X, RefreshCw, Map as MapIcon } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Card, Empty, ErrorBanner, Pill, SectionLabel, toneFromLabel } from "@/components/ui";
import { GlyphIcon, EntityChip, type EntityKind } from "@/components/EntityChip";
import { communityColor, hexToRgba } from "@/lib/palette";
import { cn } from "@/lib/utils";

// --- Explore mesh (UI redesign workstream 4) --------------------------------
// A freeform typed ego network: the focused entity centered (dashed halo, never
// a hub), its 1-hop neighbors on a radial ring, typed by SHAPE + --et-<kind>
// color. Backed by GET /api/xref/neighbors. Any node is a doorway — single
// click re-centers (push ?focus=), double click opens the entity's page.
type MeshNode = {
  kind: string; label: string; href: string | null; edge: string; token: string;
  // Memory nodes carry their OKF metadata (from GET /api/xref/neighbors): the
  // concept_type sub-captions each diamond, the domain groups same-domain
  // diamonds under a dashed hull.
  domain?: string | null; concept_type?: string | null;
  // hop = 1 (direct) or 2 (a neighbor's neighbor, drawn smaller/dimmer on the
  // outer ring). `via` is the first-hop token a second-hop node hangs off, so
  // the mesh draws its edge to the right parent instead of the center.
  hop?: number; via?: string | null;
};
// The center additionally carries last_motion (task rows only) for the
// Selected card; it never appears on neighbors.
type MeshCenter = MeshNode & { last_motion?: string | null };
// The TRUE mesh: every edge PRISM found among the collected node set (center
// + hop1 + hop2), not just center-touching spokes -- memory<->memory
// wikilinks, code<->code calls, session<->gate, session<->code, plus the
// implicit spokes themselves. `from`/`to` are node tokens (see MeshNode.token).
type MeshEdge = { from: string; to: string; label: string };
type MeshData = { center: MeshCenter; neighbors: MeshNode[]; edges: MeshEdge[] };
// The 6 canonical node shapes the mesh draws; anything else falls back to a
// neutral dot. Mirrors EntityChip's EntityKind + the --et-* token set.
const MESH_KINDS: EntityKind[] = ["code", "memory", "task", "test", "gate", "session"];
const isMeshKind = (k: string): k is EntityKind => (MESH_KINDS as string[]).includes(k);

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

// Mesh is the front door (owner direction: "the full map stays one click
// away, pre-filtered — never the front door"). The last focused token is
// persisted here so a bare /explore visit picks up right where you left off.
const MESH_FOCUS_KEY = "prism-mesh-focus";

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

  // Mesh focus lives in the URL (?focus=<token>) so it's shareable + back-
  // navigable. When set, the mesh is the centerpiece. Empty no longer means
  // "show the Sigma map" — that's now an explicit opt-in (wantFullMap) via
  // the Full map button; a bare visit resolves a default focus instead (see
  // the default-focus effect below), so the mesh is the front door.
  const navigate = useNavigate();
  const [sp, setSp] = useSearchParams();
  const focus = sp.get("focus");
  // True only once the owner explicitly clicks "Full map" — the ONLY way to
  // reach the old Sigma canvas now. Any subsequent focus (search pick, mesh
  // click, default-resolution) clears it so the mesh takes back over.
  const [wantFullMap, setWantFullMap] = useState(false);
  const setFocus = useCallback((token: string | null, opts?: { replace?: boolean }) => {
    if (token) setWantFullMap(false);
    setSp((prev) => {
      const n = new URLSearchParams(prev);
      if (token) n.set("focus", token); else n.delete("focus");
      return n;
    }, opts);
  }, [setSp]);
  // The focused entity's human label, reported up from the mesh once its
  // neighborhood loads — used to pre-seed the full-map search (delta 3).
  const [focusLabel, setFocusLabel] = useState<string | null>(null);
  // Mesh reach: 1 hop (direct ego net) or 2 hops (each neighbor's own ring,
  // smaller/dimmer). Defaults to 2 per the artifact header ("2 hops" primary).
  const [hops, setHops] = useState<1 | 2>(2);

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

  // Persist the last focused token — the first rung of the default-focus
  // ladder below, so a bare /explore visit picks up where you left off.
  useEffect(() => { if (focus) window.localStorage.setItem(MESH_FOCUS_KEY, focus); }, [focus]);

  // Default-focus resolution: a bare visit (no ?focus, no deep-link) must
  // land ON the mesh, never the Sigma canvas. Ladder: (1) the last focus
  // persisted locally, (2) the most recently updated non-done task, (3) the
  // most recent session. Runs once per project; guarded so it never fires
  // again after an explicit Full-map click clears the focus.
  const defaultResolvedProjectRef = useRef<string | null>(null);
  const [resolvingDefault, setResolvingDefault] = useState(!deepLink);
  useEffect(() => {
    if (deepLink || focus) return;
    if (defaultResolvedProjectRef.current === project) return;
    defaultResolvedProjectRef.current = project;
    let alive = true;
    setResolvingDefault(true);
    (async () => {
      try {
        const stored = window.localStorage.getItem(MESH_FOCUS_KEY);
        if (stored) { setFocus(stored, { replace: true }); return; }
        try {
          const { tasks } = await api.get<{ tasks: { id: string; status?: string; updated_at?: string }[] }>(
            `/api/tasks?project=${project}`);
          const open = (tasks ?? []).filter((t) => t.id && (t.status ?? "").toLowerCase() !== "done");
          if (open.length) {
            const best = open.reduce((a, b) => ((b.updated_at ?? "") > (a.updated_at ?? "") ? b : a));
            setFocus(best.id, { replace: true });
            return;
          }
        } catch { /* fall through to sessions */ }
        try {
          const { outcomes } = await api.get<{ outcomes: { session_id?: string }[] }>(
            `/api/sessions?project=${project}&limit=1`);
          const sid = outcomes?.[0]?.session_id;
          if (sid) { setFocus(sid, { replace: true }); return; }
        } catch { /* no sessions either — fall through to the empty state */ }
      } finally {
        if (alive) setResolvingDefault(false);
      }
    })();
    return () => { alive = false; };
  }, [project, deepLink, focus, setFocus]);

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

  // "Full map" carries the mesh's focus context into the Sigma map. The viewer
  // takes no focus/filter URL param (routes/graph_static.py drives it purely by
  // postMessage), so the honest best-effort is to seed the page search with the
  // focused entity's label and run it: that steers the WebGL canvas to
  // highlight the matching subgraph via the existing prism:search bridge. Falls
  // back to the raw focus token when the label hasn't loaded yet.
  const openFullMap = () => {
    const q = (focusLabel || focus || "").trim();
    setWantFullMap(true);
    setFocus(null);
    if (q) { setInput(q); run(q); }
  };

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
    setFocus(file);  // a search/subgraph pick focuses the mesh on that entity
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
          <SectionLabel>{wantFullMap ? "Graph" : "Explore"}</SectionLabel>
          <span className="text-xs opacity-50 ml-1">
            {focus
              ? "Freeform mesh — knowledge links knowledge, code calls code, sessions touch gates. Click a node to wander, double-click to open it."
              : wantFullMap
              ? (data?.mode === "focus"
                ? "highlighting your matches — scroll to explore, click empty space to clear"
                : "whole graph, colored by community — type a query to focus it")
              : resolvingDefault
              ? "finding somewhere to start…"
              : "search or pick an entity to explore"}
          </span>
          {focus && (
            <div className="ml-auto flex items-center gap-2">
              {/* Reach toggle (artifact header actions): 2 hops is primary. */}
              <div className="inline-flex rounded-md border border-[color:var(--border-default)] overflow-hidden">
                {([1, 2] as const).map((h) => (
                  <button
                    key={h}
                    onClick={() => setHops(h)}
                    title={h === 1 ? "Direct neighbors only" : "Neighbors and their neighbors"}
                    className={cn(
                      "px-2.5 py-1 text-2xs uppercase tracking-wider transition-colors",
                      hops === h
                        ? "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)]"
                        : "bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] text-[color:var(--text-secondary)]",
                    )}>
                    {h} hop{h === 1 ? "" : "s"}
                  </button>
                ))}
              </div>
              <button
                onClick={openFullMap}
                title="Open the full graph map, pre-filtered to this focus"
                className="inline-flex items-center gap-1 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] px-2 py-1 text-2xs uppercase tracking-wider">
                <MapIcon className="w-3 h-3" /> Full map
              </button>
            </div>
          )}
        </div>
        {focus ? (
          <Mesh token={focus} project={project} hops={hops} onFocus={setFocus}
            onOpen={(href) => navigate(href)} onCenter={setFocusLabel} />
        ) : wantFullMap ? (
          viewerUrl ? (
            <iframe
              ref={iframeRef}
              src={viewerUrl}
              className="w-full flex-1 min-h-0 border-0 rounded-b-md"
              style={{ background: "#0f0f1a" }}
            />
          ) : (
            <div className="px-5 pb-5"><Empty>No graph yet — rebuild on /graph.</Empty></div>
          )
        ) : resolvingDefault ? (
          <div className="px-5 pb-5 text-xs opacity-60">Finding somewhere to start…</div>
        ) : (
          // The full map is a deliberate opt-in (button above), never the
          // front door — the direction this mesh replaces a hairball with.
          <div className="px-5 pb-5"><Empty>Search or pick an entity to explore.</Empty></div>
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

// ===========================================================================
// Mesh — the freeform typed ego network. Center + 1-hop radial neighbors,
// shapes/colors per --et-<kind>. Single click = re-center (wander); double
// click = open the entity. Filter chips toggle kinds; legend maps shape→type.
// ===========================================================================
const MESH_W = 880, MESH_H = 520, MESH_CX = MESH_W / 2, MESH_CY = MESH_H / 2;

const meshFill = (kind: string) => (isMeshKind(kind) ? `var(--et-${kind})` : "var(--text-faint)");
const trunc = (s: string, n = 20) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

// One typed node drawn at (x,y). Shapes mirror the design artifact's node()
// draws exactly so a mesh node and an EntityChip glyph read as the same type.
function MeshShape({ kind, x, y, r }: { kind: string; x: number; y: number; r: number }) {
  const f = meshFill(kind);
  switch (kind) {
    case "code": return <rect x={x - r} y={y - r} width={2 * r} height={2 * r} rx={3} fill={f} />;
    case "memory": return <path d={`M${x} ${y - r - 2} L${x + r + 2} ${y} L${x} ${y + r + 2} L${x - r - 2} ${y} Z`} fill={f} />;
    case "task": return <rect x={x - r - 3} y={y - r + 2} width={2 * r + 6} height={2 * r - 4} rx={6} fill={f} />;
    case "test": return <path d={`M${x} ${y - r - 1} L${x + r + 1} ${y + r} L${x - r - 1} ${y + r} Z`} fill={f} />;
    case "gate": return <path d={`M${x} ${y - r - 1} L${x + r} ${y - r / 2} L${x + r} ${y + r / 2} L${x} ${y + r + 1} L${x - r} ${y + r / 2} L${x - r} ${y - r / 2} Z`} fill={f} />;
    default: return <circle cx={x} cy={y} r={r} fill={f} />;  // session + fallback
  }
}

type Placed = MeshNode & { x: number; y: number; hop: number; r: number };
type Layout = { placed: Placed[]; posByToken: Map<string, { x: number; y: number }> };

// Hop-aware radial layout: first-hop neighbors ring the center (r=12); each
// second-hop node sits on an outer ring (r=8) in a small arc around ITS parent's
// angle, so the two hops read as concentric growth rather than one flat blob.
// Deterministic — no physics sim — so labels stay legible and stable.
function layoutMesh(neighbors: MeshNode[]): Layout {
  const hop1 = neighbors.filter((n) => (n.hop ?? 1) === 1);
  const hop2 = neighbors.filter((n) => n.hop === 2);
  const n1 = hop1.length;
  const R1 = n1 <= 6 ? 152 : 182;
  const angle1 = (i: number) => (i / Math.max(n1, 1)) * Math.PI * 2 - Math.PI / 2;
  const placed1: Placed[] = hop1.map((nb, i) => {
    const theta = angle1(i);
    return { ...nb, x: MESH_CX + Math.cos(theta) * R1, y: MESH_CY + Math.sin(theta) * R1, hop: 1, r: 12 };
  });
  const angleByToken = new Map<string, number>();
  hop1.forEach((nb, i) => angleByToken.set(nb.token, angle1(i)));

  const byParent = new Map<string, MeshNode[]>();
  for (const nb of hop2) {
    const v = nb.via ?? "";
    const g = byParent.get(v) ?? []; g.push(nb); byParent.set(v, g);
  }
  const R2 = 250;
  const placed2: Placed[] = [];
  let orphan = 0;  // parents filtered out of view — fan these around the ring
  for (const [via, kids] of byParent) {
    const base = angleByToken.get(via);
    kids.forEach((nb, j) => {
      let theta: number;
      if (base === undefined) {
        theta = (orphan++ / Math.max(hop2.length, 1)) * Math.PI * 2 - Math.PI / 2;
      } else {
        const spread = Math.min(Math.PI / 2.6, kids.length * 0.17);
        const off = kids.length === 1 ? 0 : (j / (kids.length - 1) - 0.5) * spread;
        theta = base + off;
      }
      placed2.push({ ...nb, x: MESH_CX + Math.cos(theta) * R2, y: MESH_CY + Math.sin(theta) * R2, hop: 2, r: 8 });
    });
  }
  const posByToken = new Map<string, { x: number; y: number }>();
  placed1.forEach((p) => posByToken.set(p.token, { x: p.x, y: p.y }));
  return { placed: [...placed1, ...placed2], posByToken };
}

type Hull = { domain: string; x: number; y: number; w: number; h: number };

// A rounded bounding box per domain that has 2+ FIRST-HOP memory diamonds.
// Padded to clear each diamond plus its label + type sub-caption; drawn behind
// the edges so it reads as a backdrop, per the artifact's hull(). Second-hop
// diamonds are excluded — the hull is the inner ring's Understand grouping.
function meshHulls(placed: Placed[]): Hull[] {
  const byDom = new Map<string, Placed[]>();
  for (const p of placed) {
    if (p.kind !== "memory" || p.hop !== 1 || !p.domain) continue;
    const g = byDom.get(p.domain) ?? [];
    g.push(p); byDom.set(p.domain, g);
  }
  const PAD_X = 34, PAD_TOP = 30, PAD_BOTTOM = 46;  // extra below for captions
  const out: Hull[] = [];
  for (const [domain, pts] of byDom) {
    if (pts.length < 2) continue;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const minX = Math.min(...xs) - PAD_X, maxX = Math.max(...xs) + PAD_X;
    const minY = Math.min(...ys) - PAD_TOP, maxY = Math.max(...ys) + PAD_BOTTOM;
    out.push({ domain, x: minX, y: minY, w: maxX - minX, h: maxY - minY });
  }
  return out;
}

// "2h ago" style relative time from an ISO timestamp — the Selected card's
// Last motion. Returns null for a blank/unparseable stamp (omit honestly).
function relTime(iso?: string | null): string | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function Mesh({ token, project, hops, onFocus, onOpen, onCenter }: {
  token: string; project: string; hops: 1 | 2;
  onFocus: (t: string) => void; onOpen: (href: string) => void;
  onCenter?: (label: string) => void;
}) {
  const [data, setData] = useState<MeshData | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const clickTimer = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true); setErr(null);
    // 1 hop takes a wider direct fan (limit 48); 2 hops keeps the inner ring
    // tight (limit 24) so the outer ring has room to breathe.
    const limit = hops === 2 ? 24 : 48;
    api.get<MeshData>(`/api/xref/neighbors?token=${encodeURIComponent(token)}&project=${encodeURIComponent(project)}&limit=${limit}&hops=${hops}`)
      .then((d) => { if (alive) { setData(d); setHidden(new Set()); onCenter?.(d.center.label); } })
      .catch((e) => { if (alive) setErr(String(e?.message || e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [token, project, hops, onCenter]);

  const center = data?.center;

  const visible = useMemo(
    () => (data?.neighbors ?? []).filter((n) => !hidden.has(n.kind)),
    [data, hidden]);
  // Group same-domain memory diamonds so they land adjacent on the ring
  // (a prerequisite for a tight domain hull). Memory nodes lead, clustered by
  // domain; every other kind keeps its order behind them.
  const ordered = useMemo(() => {
    const byDom = new Map<string, MeshNode[]>();
    const rest: MeshNode[] = [];
    for (const n of visible) {
      if (n.kind === "memory" && n.domain) {
        const g = byDom.get(n.domain) ?? [];
        g.push(n); byDom.set(n.domain, g);
      } else rest.push(n);
    }
    return [...[...byDom.values()].flat(), ...rest];
  }, [visible]);
  const { placed } = useMemo(() => layoutMesh(ordered), [ordered]);
  // A dashed rounded hull behind each domain that has 2+ memory diamonds in
  // view — the Understand wiki's grouping drawn into the mesh (artifact hull()).
  const hulls = useMemo(() => meshHulls(placed), [placed]);
  const kindsPresent = useMemo(() => {
    const s = new Set<string>();
    (data?.neighbors ?? []).forEach((n) => s.add(n.kind));
    return MESH_KINDS.filter((k) => s.has(k));
  }, [data]);

  // Render position for EVERY currently-drawn node (center + placed hop1/2),
  // keyed by token — this is what lets an edge connect ANY two nodes, not
  // just spokes off the center. Nodes hidden by the kind filter are absent
  // from `placed`, so their edges naturally drop out below.
  const renderPos = useMemo(() => {
    const m = new Map<string, { x: number; y: number }>();
    if (center) m.set(center.token, { x: MESH_CX, y: MESH_CY });
    placed.forEach((p) => m.set(p.token, { x: p.x, y: p.y }));
    return m;
  }, [placed, center]);
  // The TRUE mesh edge list — spokes AND neighbor-to-neighbor links — pruned
  // to only the ones whose both endpoints are currently placed on the canvas.
  const renderEdges = useMemo(
    () => (data?.edges ?? []).filter(
      (e) => e.from !== e.to && renderPos.has(e.from) && renderPos.has(e.to)),
    [data, renderPos]);

  // Stats line (artifact): counts live from the drawn mesh. nodes = center +
  // every visible neighbor; edges = the true mesh edge count (spokes plus
  // neighbor-to-neighbor links) currently on the canvas; domains = distinct
  // Understand domains among the visible memory diamonds (plus the center
  // when it is itself a concept).
  const domainCount = useMemo(() => {
    const s = new Set<string>();
    if (data?.center.kind === "memory" && data.center.domain) s.add(data.center.domain);
    visible.forEach((n) => { if (n.kind === "memory" && n.domain) s.add(n.domain); });
    return s.size;
  }, [data, visible]);
  // Degree for the Selected card: direct (hop 1) vs reached-at-2-hops.
  const directCount = useMemo(
    () => (data?.neighbors ?? []).filter((n) => (n.hop ?? 1) === 1).length, [data]);
  const twoHopCount = useMemo(
    () => (data?.neighbors ?? []).filter((n) => n.hop === 2).length, [data]);
  const lastMotion = relTime(data?.center.last_motion);

  // Single click re-centers (wander the mesh); double click opens the page.
  // A short timer disambiguates the two without a jarring double-fire.
  const onNodeClick = (nb: MeshNode) => {
    if (clickTimer.current) window.clearTimeout(clickTimer.current);
    clickTimer.current = window.setTimeout(() => onFocus(nb.token), 200);
  };
  const onNodeDbl = (nb: MeshNode) => {
    if (clickTimer.current) { window.clearTimeout(clickTimer.current); clickTimer.current = null; }
    if (nb.href) onOpen(nb.href);
  };
  const toggleKind = (k: string) =>
    setHidden((prev) => {
      const n = new Set(prev);
      if (n.has(k)) n.delete(k); else n.add(k);
      return n;
    });

  return (
    <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[1fr_300px] gap-3 px-5 pb-4 overflow-hidden">
      {/* canvas */}
      <div className="flex flex-col rounded-lg border border-[color:var(--border-default)] bg-[color:var(--surface-2)] overflow-hidden min-h-0">
        {/* filter chips */}
        <div className="flex flex-wrap items-center gap-1.5 px-3 py-2 border-b border-[color:var(--border-default)]">
          {kindsPresent.map((k) => {
            const on = !hidden.has(k);
            return (
              <button key={k} onClick={() => toggleKind(k)}
                className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-2xs font-medium capitalize transition-colors",
                  on ? "border-[color:var(--border-strong)] bg-[color:var(--surface-1)] text-[color:var(--text-primary)]"
                     : "border-[color:var(--border-default)] bg-[color:var(--surface-2)] text-[color:var(--text-secondary)] opacity-50")}>
                <GlyphIcon kind={k} size={10} /> {k}
              </button>
            );
          })}
          <span className="ml-auto text-2xs opacity-60 tabular-nums font-mono truncate max-w-[52%]">
            {1 + visible.length} nodes · {renderEdges.length} edges · {domainCount} domain{domainCount === 1 ? "" : "s"}
            {center ? <> · selected <span className="text-[color:var(--text-primary)]">{trunc(center.label, 22)}</span></> : null}
          </span>
        </div>

        <div className="flex-1 min-h-0 flex items-center justify-center overflow-hidden p-2">
          {loading && !data ? (
            <div className="p-6 text-xs opacity-60">Loading neighborhood…</div>
          ) : err ? (
            <div className="p-4"><ErrorBanner>{err}</ErrorBanner></div>
          ) : !center ? (
            <div className="p-6"><Empty>Nothing to center on.</Empty></div>
          ) : (
            <svg viewBox={`0 0 ${MESH_W} ${MESH_H}`} preserveAspectRatio="xMidYMid meet"
              className="w-full h-full" role="img"
              aria-label={`Mesh around ${center.label}`}>
              {/* domain hulls behind everything — the Understand grouping,
                  --et-memory at low opacity with a dashed stroke */}
              {hulls.map((h, i) => (
                <g key={`h${i}`}>
                  <rect x={h.x} y={h.y} width={h.w} height={h.h} rx={18}
                    fill="var(--et-memory)" opacity={0.07} />
                  <rect x={h.x} y={h.y} width={h.w} height={h.h} rx={18}
                    fill="none" stroke="var(--et-memory)" strokeDasharray="4 4"
                    opacity={0.35} />
                  <text x={h.x + 12} y={h.y + 16} fontSize={9}
                    letterSpacing="0.1em" fill="var(--et-memory)" opacity={0.9}
                    style={{ textTransform: "uppercase" }}>
                    DOMAIN · {h.domain}
                  </text>
                </g>
              ))}
              {/* edges first, under the nodes. A TRUE mesh: any two placed
                  nodes can edge to each other, not just center-touching
                  spokes. A spoke (either end is the center) draws bolder +
                  brighter than a neighbor-to-neighbor link, so the inner
                  ring still reads as the doorway without spokes dominating. */}
              {renderEdges.map((e) => {
                const a = renderPos.get(e.from)!;
                const b = renderPos.get(e.to)!;
                const spoke = center && (e.from === center.token || e.to === center.token);
                const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
                return (
                  <g key={`${e.from}->${e.to}`}>
                    <line x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                      stroke={spoke ? "var(--border-strong)" : "var(--border-default)"}
                      strokeWidth={spoke ? 1.1 : 0.9} opacity={spoke ? 1 : 0.6} />
                    <text x={mx} y={my - 3} textAnchor="middle" fontSize={spoke ? 11 : 9.5}
                      fill="var(--text-faint)" opacity={spoke ? 1 : 0.8}>{e.label}</text>
                  </g>
                );
              })}
              {/* center — dashed halo marks selection; a doorway, not a hub */}
              <circle cx={MESH_CX} cy={MESH_CY} r={23} fill="none"
                stroke={meshFill(center.kind)} strokeWidth={1.5}
                strokeDasharray="3 3" opacity={0.8} />
              <g style={{ cursor: center.href ? "pointer" : "default" }}
                onDoubleClick={() => center.href && onOpen(center.href)}>
                <MeshShape kind={center.kind} x={MESH_CX} y={MESH_CY} r={16} />
                <text x={MESH_CX} y={MESH_CY + 34} textAnchor="middle" fontSize={12}
                  fontWeight={650} fill="var(--text-primary)">{trunc(center.label)}</text>
                {center.kind === "memory" && center.concept_type && (
                  <text x={MESH_CX} y={MESH_CY + 47} textAnchor="middle" fontSize={9}
                    letterSpacing="0.06em" fill={meshFill("memory")}
                    style={{ textTransform: "uppercase" }}>{center.concept_type}</text>
                )}
              </g>
              {/* neighbors. Second-hop nodes are smaller (r=8) and dimmed so the
                  focus stays on the inner ring; both hops stay clickable. */}
              {placed.map((nb, i) => {
                const two = nb.hop === 2;
                return (
                  <g key={`n${i}`} style={{ cursor: "pointer" }} opacity={two ? 0.62 : 1}
                    onClick={() => onNodeClick(nb)} onDoubleClick={() => onNodeDbl(nb)}>
                    <title>{`${nb.label} — ${nb.edge}${two ? " (2 hops)" : ""} (click to center, double-click to open)`}</title>
                    <MeshShape kind={nb.kind} x={nb.x} y={nb.y} r={nb.r} />
                    <text x={nb.x} y={nb.y + (two ? 20 : 26)} textAnchor="middle" fontSize={two ? 9.5 : 11}
                      fill="var(--text-secondary)">{trunc(nb.label, two ? 13 : 16)}</text>
                    {/* concept-type sub-caption under the diamond (decision/
                        convention/expertise/anti-pattern/principle) */}
                    {nb.kind === "memory" && nb.concept_type && !two && (
                      <text x={nb.x} y={nb.y + 37} textAnchor="middle" fontSize={8.5}
                        letterSpacing="0.06em" fill={meshFill("memory")}
                        style={{ textTransform: "uppercase" }}>
                        {nb.concept_type}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
          )}
        </div>

        {/* legend */}
        <div className="flex flex-wrap gap-3 px-3 py-2 border-t border-[color:var(--border-default)] text-2xs text-[color:var(--text-faint)]">
          {MESH_KINDS.map((k) => (
            <span key={k} className="inline-flex items-center gap-1 capitalize">
              <GlyphIcon kind={k} size={10} /> {k}
            </span>
          ))}
          <span className="ml-auto">dashed halo = selected · click a node to re-center</span>
        </div>
      </div>

      {/* rail */}
      <div className="hidden lg:flex flex-col gap-3 min-h-0 overflow-y-auto">
        {center && (
          <Card className="!p-4">
            <SectionLabel>Selected</SectionLabel>
            <div className="mt-2 flex items-center gap-2">
              <GlyphIcon kind={isMeshKind(center.kind) ? center.kind : "session"} />
              <span className="text-sm truncate">{center.label}</span>
            </div>
            <div className="mt-3 space-y-1 text-2xs">
              <div className="flex items-center gap-2">
                <span className="opacity-50 uppercase tracking-wider w-[72px] shrink-0">Degree</span>
                <span className="tabular-nums text-[color:var(--text-secondary)]">
                  {directCount} direct{twoHopCount > 0 ? ` · ${twoHopCount} at 2 hops` : ""}
                </span>
              </div>
              {lastMotion && (
                <div className="flex items-center gap-2">
                  <span className="opacity-50 uppercase tracking-wider w-[72px] shrink-0">Last motion</span>
                  <span className="text-[color:var(--text-secondary)]">{lastMotion}</span>
                </div>
              )}
            </div>
            {center.href && (
              <button onClick={() => onOpen(center.href!)}
                className="mt-3 text-xs text-[color:var(--accent-teal-fg)] hover:underline">
                Open detail →
              </button>
            )}
          </Card>
        )}
        <Card className="!p-4 min-h-0">
          <SectionLabel>Connections · {visible.length}</SectionLabel>
          <div className="mt-2 flex flex-col gap-1.5">
            {visible.length === 0 ? (
              <Faint>No connections in view.</Faint>
            ) : visible.map((nb, i) => (
              <div key={i} className="flex items-center gap-2 min-w-0">
                <button onClick={() => onFocus(nb.token)} title="Re-center on this"
                  className="min-w-0 flex-1 text-left">
                  <EntityChip kind={isMeshKind(nb.kind) ? nb.kind : "session"}
                    label={trunc(nb.label, 22)} className="max-w-full" />
                </button>
                <span className="shrink-0 text-2xs opacity-40 lowercase">{nb.edge}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
