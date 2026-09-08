import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Compass, Network, Search, CornerDownLeft, ArrowRight, ArrowLeft, X, RefreshCw, Map as MapIcon } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import ArchifyMaps from "@/components/maps/ArchifyMaps";
import { Card, Empty, ErrorBanner, Pill, SectionLabel, toneFromLabel } from "@/components/ui";
import Mesh from "@/components/Mesh";
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
  // A THIRD READING OF THE SAME CODE GRAPH, not a second widget beside it.
  // The mesh shows reach, the Sigma map shows the whole hairball, and this
  // shows the authored architecture — all of them draw graph.db, so they
  // belong on one surface behind one control rather than stacked as
  // separate panels.
  const [wantArchitecture, setWantArchitecture] = useState(false);
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
  //
  // ?focus= is the MESH's generic token param -- it carries files, but also
  // task ids, session ids and concept ids (the mesh writes whatever node you
  // wander onto, the default-focus ladder writes a task, /understand writes a
  // concept). focusSeed() below treats its argument as a FILE and seeds
  // /api/brain/understand with it, so a non-file token landing here used to
  // misfire a code-symbol lookup: the backend echoed the raw id back as a
  // phantom kind:"file" node, which dumped the bare uuid into the search box,
  // showed 1 node / 0 edges / 0 communities in the stat strip, and steered the
  // Sigma canvas at a file that does not exist -- code-explore chrome wrapped
  // around a task mesh. Only take the code-seed path when the token is
  // actually file-shaped (a path separator or a real extension), or when an
  // explicit &symbol= says a code seed was meant.
  const deepLink = useMemo(() => {
    const p = new URLSearchParams(window.location.search);
    const file = p.get("focus");
    if (!file) return null;
    const symbol = p.get("symbol");
    const fileShaped = file.includes("/") || file.includes("\\") || /\.[A-Za-z0-9]{1,8}$/.test(file);
    return fileShaped || symbol ? { file, symbol } : null;
  }, []);

  // Explicit /brain?session=<id> / /brain?task=<id> deep links (the /live
  // graph's EXPLORE hop, owner ask: "every node can click through to this
  // explore session where we can see the content used to drive it
  // visually"). Deliberately SEPARATE param names from ?focus= above --
  // that one is always treated as a FILE by deepLink/focusSeed, so a raw
  // task/session id landing there would misfire a code-symbol seed lookup.
  // Read once on mount, same pattern as deepLink; normalized straight into
  // the mesh's own ?focus=<token> token space in the effect below (Mesh
  // accepts any xref token generically -- see the `focus` state and
  // MeshFocus prop just above), so it never lingers as a second URL param.
  const paramFocusSeed = useMemo(() => {
    const p = new URLSearchParams(window.location.search);
    return p.get("session") || p.get("task") || null;
  }, []);

  useEffect(() => { if (!deepLink && !paramFocusSeed) run(""); }, [run, deepLink, paramFocusSeed]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (deepLink) focusSeed(deepLink.file, deepLink.symbol); }, []);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (paramFocusSeed && !focus) setFocus(paramFocusSeed, { replace: true }); }, []);

  // NO AUTO-FOCUS. A bare visit used to probe the board and silently centre
  // the mesh on whichever task moved last, so Explore opened on a task
  // nobody asked for and the architecture flashed past on the way (owner:
  // "i dont neeed start with a task or any of that the code arch is in the
  // graph isnt it?"). The front door now simply draws the code graph and
  // waits — see StartHere. A focus still arrives three honest ways: a click
  // on the architecture, ?focus=<token>, or the ?task=/?session= hop above.

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
  // Does the drill-down strip below have anything real in it yet? An empty
  // panel is worse than no panel: it takes the room and reads as broken.
  const hasClusters = !!inView && inView.items.length > 0;
  const hasSubgraph = data?.mode === "focus" && data.nodes.length > 0;

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
          <SectionLabel>
            {wantArchitecture ? "Architecture" : wantFullMap ? "Graph" : "Explore"}
          </SectionLabel>
          <span className="text-xs opacity-50 ml-1">
            {focus
              ? "Freeform mesh — knowledge links knowledge, code calls code, sessions touch gates. Click a node to wander, double-click to open it."
              : wantFullMap
              ? (data?.mode === "focus"
                ? "highlighting your matches — scroll to explore, click empty space to clear"
                : "whole graph, colored by community — type a query to focus it")
              : "The code graph, drawn as its architecture. Click a cluster to explore it."}
          </span>
          {/* One control for the readings of this one graph. */}
          <button
            onClick={() => setWantArchitecture((v) => !v)}
            title="The authored architecture drawn from this same graph"
            className={cn(
              "ml-auto inline-flex items-center gap-1 rounded-md border px-2 py-1 text-2xs uppercase tracking-wider transition-colors",
              wantArchitecture
                ? "border-[color:var(--accent-teal-fg)] bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)]"
                : "border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)]",
            )}>
            <MapIcon className="w-3 h-3" /> Architecture
          </button>
          {focus && !wantArchitecture && (
            <div className="flex items-center gap-2">
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
        {wantArchitecture ? (
          <div className="px-5 pb-5 min-h-0 overflow-y-auto">
            <ArchifyMaps project={project} kind="code" />
          </div>
        ) : focus ? (
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
        ) : (
          // THE FRONT DOOR IS THE ARCHITECTURE, NOT A BLANK CANVAS and not a
          // list of tasks. The mesh needs a focus to draw anything, so this
          // draws the code graph itself until one is chosen — full height,
          // because a map you cannot read is not a map. The Sigma hairball
          // stays a deliberate opt-in; this is not that.
          <StartHere project={project} onPick={(token) => setFocus(token)} />
        )}
      </Card>

      {/* Panels in a bounded strip that scrolls internally — the FULL-MAP /
          search workflow's drill-down surface. The mesh view owns its own
          rail, so when a focus is set this strip stays out of the way.

          IT ALSO STAYS OUT OF THE WAY WHEN IT HAS NOTHING TO SAY. The strip
          rendered unconditionally, so the front door carried a permanently
          empty "Context bundle" card reading "Type a query, then pick a
          result…" — an instruction where content belongs, eating half the
          width and up to 32vh of the height that the architecture needed
          (owner pointed straight at it: "this does not belong here"). It now
          appears only once something has actually filled it. */}
      {!focus && (hasClusters || hasSubgraph || selected) && (
      <div className={cn("shrink-0 overflow-y-auto grid grid-cols-1 gap-4 items-start",
                         selected && (hasClusters || hasSubgraph) && "lg:grid-cols-[1fr_1fr]")}
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

        {/* RESULT — context for whatever you click on the left. Only once
            something IS clicked: with nothing selected this card had no
            content to show and rendered its own instructions instead. */}
        {selected && (
          <div className="min-w-0">
            <ContextRail sel={sel} selected={selected} mode={data?.mode} />
          </div>
        )}
      </div>
      )}
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


// ─────────────────────────────────────────────────────────────────────────
// StartHere — THE FRONT DOOR IS THE ARCHITECTURE. Explore is the code
// graph, so a bare visit draws the code graph: the architecture built from
// graph.db, filling the card. Clicking a cluster centres the mesh on that
// cluster's most-connected symbol, which is how you get from the whole
// system to one neighbourhood.
//
// It used to open on a "Start with a task" chip ladder above a letterboxed
// map, and a silent probe auto-centred the mesh on whichever task moved
// last (owner: "i dont neeed start with a task or any of that the code arch
// is in the graph isnt it?"). Two things were wrong with that. Explore is
// not a task surface — the task-centred mesh belongs on the task page,
// where it now lives — and the auto-focus meant the architecture flashed up
// and was replaced a second later by a task nobody asked for. Tasks are
// still one click from here via the mesh, and ?focus=<any token> still
// opens any entity directly.
// ─────────────────────────────────────────────────────────────────────────

function StartHere({ project, onPick }: {
  project: string; onPick: (token: string) => void;
}) {
  const [communities, setCommunities] = useState<Community[]>([]);

  useEffect(() => {
    let alive = true;
    // Clicking a cluster in the drawing has to land on a real token, and a
    // cluster id is not one — the communities carry the symbols that are.
    api.get<{ communities: Community[] }>(`/api/graph/communities?project=${project}`)
      .then((r) => { if (alive) setCommunities(r.communities ?? []); })
      .catch(() => { if (alive) setCommunities([]); });
    return () => { alive = false; };
  }, [project]);

  // archify draws each cluster with id `c<communityId>-<slugged label>`.
  const onMapNode = (nodeId: string) => {
    const m = /^c(\d+)-/.exec(nodeId);
    if (!m) return;
    const c = communities.find((x) => String(x.id) === m[1]);
    const token = c?.top_entities?.find((e) => e && e.trim());
    if (token) onPick(token.replace(/\(\)$/, ""));
  };

  return (
    <div className="px-5 pb-5 flex-1 min-h-0 flex flex-col">
      <ArchifyMaps project={project} kind="code" onNodeSelect={onMapNode} fill />
    </div>
  );
}
