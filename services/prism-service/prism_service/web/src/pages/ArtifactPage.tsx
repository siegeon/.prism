import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY,
} from "d3-force";
import { ArrowLeft, ArrowRight, Compass, FileCode, Network, Boxes, BookOpen } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Card, Empty, ErrorBanner, Pill, SectionLabel } from "@/components/ui";
import { COMMUNITY_HEX, communityColor, hexToRgba } from "@/lib/palette";

// --- xref slice S5 - the unified artifact surface ---------------------------
// ONE destination for a resolved CODE token (a file + symbol). The xref
// resolver (slice S1) routes a code token here; this page composes the code
// path via POST /api/brain/understand (the same fused Brain+Graph retrieval
// that backs /brain), then renders it as a single readable surface:
//   1. Title + summary at the top (the symbol + its narrative/community gloss).
//   2. The grouped brain subgraph below (community-colored seed/neighbor chips
//      mirroring the canvas - same shape as ExplorePage's Subgraph).
//   3. Drill-onward links: into /brain?focus=<file>&symbol=<name> (code) and
//      across to /understand?concept=<id> (memory).
// Reached as a ROUTE (/artifact?focus=<file>&symbol=<name>), not a nav item.

type GNode = { id: string; label: string; kind: string; community?: number | null; centrality?: number; seed?: boolean };
type Community = { id: number; label: string; size: number; summary: string };
type Annotation = {
  scope_kind: "node" | "community" | "hierarchy";
  scope_id: string; name: string; purpose: string;
  provenance: string; updated_at?: string | null;
};
type Ctx = {
  entity_id: string; file: string; community?: number | null;
  outline: { name: string; kind: string; line?: number | null }[];
  references: { from: string; weight: number }[];
  call_chain: { to: string; weight: number }[];
  chunks: string[]; annotations: Annotation[];
};
type GEdge = { from: string; to: string; weight: number };
type Understanding = {
  query: string; mode: "overview" | "focus";
  nodes: GNode[]; edges: GEdge[]; communities: Community[];
  context: Ctx[]; counts: Record<string, number>; provenance: string;
};

const base = (p: string) => (p || "").replace(/\\/g, "/").split("/").pop() || p;
const SUMMARY_CAP = 280; // chars before the summary collapses to a click-to-expand

export default function ArtifactPage() {
  const [project] = useProject();
  const [params] = useSearchParams();
  // The resolver reaches this surface with the code token's coordinates.
  // `focus` (the file path) is the seed; `symbol` titles the artifact;
  // `concept` (optional) carries a real memory concept id when S1 knows one.
  const focus = params.get("focus") || "";
  const symbol = params.get("symbol") || "";
  const concept = params.get("concept") || "";

  const [data, setData] = useState<Understanding | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!focus && !symbol) { setData(null); return; }
    setLoading(true); setError(null);
    // Seed the fused retrieval on the file when we have one (precise focus on
    // the artifact's own subgraph); otherwise fall back to a symbol search.
    const body = focus
      ? { project, seed_files: [focus], label: symbol || base(focus), limit: 20, depth: 1 }
      : { project, query: symbol, limit: 20, depth: 1 };
    api.post<Understanding>("/api/brain/understand", body)
      .then(setData)
      .catch((e) => { setData(null); setError(String(e?.message || e)); })
      .finally(() => setLoading(false));
  }, [project, focus, symbol]);

  // The context bundle for the focused file is the artifact's spine - its
  // outline, callers, callees, and narrative.
  const sel = useMemo<Ctx | undefined>(() => {
    if (!data) return undefined;
    return data.context.find((c) => c.file === focus) ?? data.context[0];
  }, [data, focus]);

  // Summary precedence: the file's own narrative annotation > its community's
  // gloss > the top matched chunk. Deterministic structure first, LLM prose
  // only when it exists.
  const summary = useMemo(() => {
    const ann = sel?.annotations.find((a) => a.purpose.trim());
    if (ann) return { text: ann.purpose.trim(), source: ann.provenance };
    const comm = data?.communities.find((c) => c.summary?.trim());
    if (comm) return { text: comm.summary.trim(), source: `cluster - ${comm.label}` };
    const chunk = sel?.chunks.find((c) => c.trim());
    if (chunk) return { text: `${chunk.trim()}...`, source: "matched chunk" };
    return null;
  }, [sel, data]);

  const title = symbol || (focus ? base(focus) : "Artifact");
  // Onward into the graph: the deep-link contract S1 routes code tokens by.
  const brainHref = `/brain?focus=${encodeURIComponent(focus)}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ""}`;
  // Across to memory: prefer a real concept id from the resolver; else seed the
  // wiki with the symbol so the term still lands in the Understand surface.
  const conceptId = concept || symbol || base(focus);
  const understandHref = conceptId ? `/understand?concept=${encodeURIComponent(conceptId)}` : "/understand";

  if (!focus && !symbol) {
    return (
      <div className="p-8 w-full min-w-[720px]">
        <Empty>No artifact selected - reach this surface by clicking a resolved code token.</Empty>
      </div>
    );
  }

  return (
    <div className="p-8 space-y-6 w-full min-w-[720px]">
      {error && <ErrorBanner>{error}</ErrorBanner>}

      {/* Top row: visual graph (left) + dynamic understander (right) --
          matches the prototype's split: filtered code graph | summary rail. */}
      <div className="grid grid-cols-1 lg:grid-cols-[1.3fr_1fr] gap-6 items-start">
        <SubgraphView data={data} loading={loading} focus={focus} />
        <Card raised className="space-y-4">
          <div className="flex items-start gap-3">
            <FileCode className="w-5 h-5 mt-0.5 opacity-60 shrink-0" />
            <div className="min-w-0">
              <h1 className="text-lg font-semibold text-[color:var(--text-primary)] font-mono truncate">{title}</h1>
              {focus && <div className="text-xs opacity-50 font-mono break-all mt-0.5">{focus}</div>}
            </div>
            <span className="ml-auto shrink-0">
              <Pill tone="slate" active>{data?.provenance ?? (loading ? "..." : "-")}</Pill>
            </span>
          </div>
          {/* C4 depth layers (presentational relabel over the hierarchy). */}
          <C4Bar />
          <SummaryBlock summary={summary} loading={loading} />
          {/* Drill-onward links */}
          <div className="flex flex-wrap gap-2 pt-1">
            <DrillLink to={brainHref} icon={<Network className="w-4 h-4" />} label="Open in Brain graph" />
            <DrillLink to={understandHref} icon={<BookOpen className="w-4 h-4" />} label="Across to Understand" />
          </div>
        </Card>
      </div>

      {/* Bottom: the context bundle, full width (outline / callers / callees). */}
      <ContextSpine sel={sel} loading={loading} />
    </div>
  );
}

function SummaryBlock({ summary, loading }: { summary: { text: string; source: string } | null; loading: boolean }) {
  const [open, setOpen] = useState(false);
  if (loading && !summary) return <div className="text-sm opacity-40">Resolving artifact...</div>;
  if (!summary) {
    return <div className="text-sm opacity-50">No narrative yet - the structure below is deterministic.</div>;
  }
  const long = summary.text.length > SUMMARY_CAP;
  const shown = long && !open ? `${summary.text.slice(0, SUMMARY_CAP)}...` : summary.text;
  return (
    <div>
      <p className="text-sm leading-relaxed text-[color:var(--text-secondary)] whitespace-pre-wrap">{shown}</p>
      <div className="mt-2 flex items-center gap-3 text-[11px]">
        <span className="uppercase tracking-wider opacity-40">{summary.source}</span>
        {long && (
          <button onClick={() => setOpen((v) => !v)} className="opacity-70 hover:opacity-100 underline-offset-2 hover:underline">
            {open ? "show less" : "show more"}
          </button>
        )}
      </div>
    </div>
  );
}

// The focused brain subgraph, drawn inline as a REAL graph (not chips, not the
// heavy 17k-edge Sigma canvas): a deterministic radial layout -- seed at center,
// 1-hop neighbors on a community-grouped ring, all induced edges. This is the
// LIVE /api/brain/understand ego-graph (a few dozen edges), so it renders
// instantly. Clicking a node drills to that artifact; the context panel on the
// right stays put. No "open in graph" detour, no full-graph load.
type Pt = { x: number; y: number };
type Layout = { pos: Map<string, Pt>; deg: Map<string, number>; maxDeg: number };

// SAME d3-force recipe as /understand's concept graph (charge/link/collide
// tuned there), so navigating concept-graph -> artifact-graph reads as ONE
// visual system instead of a jarring layout swap. Computed once, then fit to
// the pane's viewBox.
function forceLayout(nodes: GNode[], edges: GEdge[], W: number, H: number): Layout {
  const ids = new Set(nodes.map((n) => n.id));
  const deg = new Map<string, number>();
  nodes.forEach((n) => deg.set(n.id, 0));
  type SN = { id: string; x?: number; y?: number };
  const links: { source: string; target: string }[] = [];
  for (const e of edges) {
    if (!ids.has(e.from) || !ids.has(e.to)) continue;
    links.push({ source: e.from, target: e.to });
    deg.set(e.from, (deg.get(e.from) ?? 0) + 1);
    deg.set(e.to, (deg.get(e.to) ?? 0) + 1);
  }
  let maxDeg = 1;
  for (const d of deg.values()) if (d > maxDeg) maxDeg = d;
  const sn: SN[] = nodes.map((n) => ({ id: n.id }));
  const sim = forceSimulation<SN>(sn)
    .force("charge", forceManyBody<SN>().strength(-260))
    .force("link", forceLink<SN, { source: string; target: string }>(links).id((d) => d.id).distance(95).strength(0.6))
    .force("center", forceCenter(0, 0))
    .force("collide", forceCollide<SN>().radius(34))
    .force("x", forceX(0).strength(0.04))
    .force("y", forceY(0).strength(0.04))
    .stop();
  for (let i = 0; i < 350; i++) sim.tick();
  // Fit the centered cloud into the viewBox with padding.
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const n of sn) {
    minX = Math.min(minX, n.x ?? 0); maxX = Math.max(maxX, n.x ?? 0);
    minY = Math.min(minY, n.y ?? 0); maxY = Math.max(maxY, n.y ?? 0);
  }
  const pad = 56;
  const fitX = (W - 2 * pad) / Math.max(1, maxX - minX);
  const fitY = (H - 2 * pad) / Math.max(1, maxY - minY);
  // Fill BOTH axes so the cloud uses the whole pane (no centered-and-tiny look
  // on wide screens), but cap each axis to 1.7x the tighter fit so a wide pane
  // spreads nodes out without extreme stretch. Node circles are fixed-radius, so
  // only positions stretch -- dots stay round.
  const tight = Math.min(fitX, fitY);
  const sx = Math.min(fitX, tight * 1.7);
  const sy = Math.min(fitY, tight * 1.7);
  // Center the cloud (so a single/degenerate node sits dead center, too).
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const pos = new Map<string, Pt>();
  for (const n of sn) {
    pos.set(n.id, { x: W / 2 + ((n.x ?? 0) - cx) * sx, y: H / 2 + ((n.y ?? 0) - cy) * sy });
  }
  return { pos, deg, maxDeg };
}

// Degree-sized radius (same idea as /understand's nodeSize, scaled for this
// smaller pane): hubs read larger, leaves compact.
const nodeRByDeg = (d: number, maxD: number) => {
  const t = Math.sqrt(d) / Math.sqrt(Math.max(1, maxD));
  return 8 + t * 14;
};

// Vivid node color from the ONE shared palette -- by community when the graph
// has community structure, else a stable per-module hue so same-folder files
// share a color (the analog of /understand coloring concepts by type). Never
// the grey null-community fallback, which read as "dead".
function codeColor(n: GNode): string {
  if (n.community !== null && n.community !== undefined) return communityColor(n.community);
  const parts = (n.id || "").replace(/\\/g, "/").split("/").filter(Boolean);
  const key = parts.length >= 2 ? parts[parts.length - 2] : parts[0] || n.id;
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return COMMUNITY_HEX[h % COMMUNITY_HEX.length];
}

function SubgraphView(
  { data, loading, focus }: { data: Understanding | null; loading: boolean; focus: string },
) {
  const navigate = useNavigate();
  // Measure the real container width so the graph fills the pane (no letterbox)
  // and the force layout spreads across the ACTUAL space on any screen size.
  const wrapRef = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(900);
  const H = 400;
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w && Math.abs(w - W) > 8) setW(Math.round(w));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [W]);
  const layout = useMemo(
    () => (data ? forceLayout(data.nodes, data.edges, W, H) : null),
    [data, W],
  );
  const pos = layout?.pos ?? new Map<string, Pt>();
  return (
    <Card className="!p-5">
      <div className="flex items-center gap-2 mb-3">
        <Boxes className="w-4 h-4 opacity-60" />
        <SectionLabel>Brain subgraph</SectionLabel>
      </div>
      {loading && !data ? (
        <div className="text-xs opacity-40">Loading subgraph...</div>
      ) : !data || data.nodes.length === 0 ? (
        <Empty>No connected subgraph for this artifact.</Empty>
      ) : (
        <>
          <div className="text-xs opacity-60 mb-2">
            {data.counts.nodes ?? data.nodes.length} files &middot;{" "}
            {data.counts.edges ?? data.edges.length} edges &middot; colored by module,
            click a node to drill.
          </div>
          <div ref={wrapRef} className="w-full">
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H}
            className="rounded-md bg-[color:var(--surface-1)]" role="img">
            <defs>
              {/* Dotted backdrop -- mirrors /understand's ReactFlow Background. */}
              <pattern id="xref-dots" width="24" height="24" patternUnits="userSpaceOnUse">
                <circle cx="1" cy="1" r="1" fill="rgba(200,180,140,0.10)" />
              </pattern>
            </defs>
            <rect width={W} height={H} fill="url(#xref-dots)" />
            {data.edges.map((e, i) => {
              const a = pos.get(e.from), b = pos.get(e.to);
              if (!a || !b) return null;
              const incident = e.from === focus || e.to === focus
                || data.nodes.some((n) => n.seed && (n.id === e.from || n.id === e.to));
              return (
                <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                  stroke={incident ? "var(--text-primary)" : "var(--border-strong)"}
                  strokeWidth={incident ? 1.5 : 1}
                  opacity={incident ? 0.55 : 0.18} />
              );
            })}
            {data.nodes.map((n) => {
              const p = pos.get(n.id);
              if (!p) return null;
              const hot = n.seed || n.id === focus;
              const r = Math.max(hot ? 11 : 7, nodeRByDeg(layout?.deg.get(n.id) ?? 0, layout?.maxDeg ?? 1));
              const col = codeColor(n);
              return (
                <g key={n.id} transform={`translate(${p.x},${p.y})`} className="cursor-pointer"
                  onClick={() => navigate(`/artifact?focus=${encodeURIComponent(n.id)}`)}>
                  <title>{n.id}</title>
                  {/* Selection glow ring -- mirrors ConceptNode's box-shadow. */}
                  {hot && (
                    <circle r={r + 4} fill="none" stroke="var(--text-primary)"
                      strokeWidth={2} opacity={0.5} />
                  )}
                  <circle r={r} fill={col}
                    stroke={hot ? "var(--text-primary)" : hexToRgba(col, 0.65)}
                    strokeWidth={hot ? 2.5 : 1.25} className="hover:brightness-125" />
                  <text textAnchor="middle" y={r + 11} fontSize={hot ? 11 : 9}
                    className="font-medium pointer-events-none"
                    fill={hot ? "var(--text-primary)" : "var(--text-secondary)"}>
                    {base(n.label || n.id)}
                  </text>
                </g>
              );
            })}
          </svg>
          </div>
        </>
      )}
    </Card>
  );
}

// C4 depth layers (Context/Container/Component/Code) -- a presentational
// relabel over the existing brain hierarchy (NFR-5). An artifact surface is the
// Code leaf, so Code reads active; the upper layers signal the navigable
// altitudes you drill through in /brain.
const C4_LAYERS = ["Context", "Container", "Component", "Code"];
function C4Bar() {
  return (
    <div className="flex flex-wrap gap-1.5">
      {C4_LAYERS.map((l, i) => (
        <span
          key={l}
          title={`C4 depth layer: ${l}`}
          className={`text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border ${
            i === C4_LAYERS.length - 1
              ? "border-[color:var(--accent-teal-ring)] text-[color:var(--accent-teal-fg)] bg-[color:var(--accent-teal-bg)]"
              : "border-[color:var(--border-default)] text-[color:var(--text-label)]"
          }`}
        >
          {l}
        </span>
      ))}
    </div>
  );
}

// The context spine - outline, callers, callees, narrative for the focused
// file. Full-width below the graph+understander, laid out as columns.
function ContextSpine({ sel, loading }: { sel?: Ctx; loading: boolean }) {
  return (
    <Card className="!p-5">
      <div className="flex items-center gap-2">
        <Compass className="w-4 h-4 opacity-60" />
        <SectionLabel>Context</SectionLabel>
      </div>
      {loading && !sel ? (
        <div className="mt-3 text-xs opacity-40">Loading context...</div>
      ) : !sel ? (
        <Empty>No context bundle for this artifact.</Empty>
      ) : (
        <div className="mt-3 grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-x-7 gap-y-4 items-start">
          <Section label={`Outline - ${sel.outline.length}`}>
            {sel.outline.length === 0 ? <Faint>none</Faint> : (
              <ul className="space-y-0.5 max-h-44 overflow-y-auto">
                {sel.outline.map((o, i) => (
                  <li key={i} className="flex items-baseline gap-2 text-xs">
                    <span className="font-mono truncate">{o.name}</span>
                    <span className="opacity-40">{o.kind && o.kind !== "unknown" ? o.kind : ""}{o.line ? ` :${o.line}` : ""}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>
          <Section label={`Callers - ${sel.references.length}`} icon={<ArrowLeft className="w-3 h-3" />}>
            {sel.references.length === 0 ? <Faint>none</Faint> :
              sel.references.map((r, i) => <Edge key={i} file={r.from} w={r.weight} />)}
          </Section>
          <Section label={`Callees - ${sel.call_chain.length}`} icon={<ArrowRight className="w-3 h-3" />}>
            {sel.call_chain.length === 0 ? <Faint>none</Faint> :
              sel.call_chain.map((r, i) => <Edge key={i} file={r.to} w={r.weight} />)}
          </Section>
          <Section label={`Narrative - ${sel.annotations.length}`}>
            {sel.annotations.length === 0 ? (
              <Faint>no annotations yet - structure above is deterministic</Faint>
            ) : (
              <ul className="space-y-2">
                {sel.annotations.map((a, i) => {
                  const isLlm = a.provenance.startsWith("claude @");
                  return (
                    <li key={i} className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] px-2.5 py-2">
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <span className="text-xs font-medium text-[color:var(--text-primary)] truncate">{a.name}</span>
                        <Pill tone={isLlm ? "violet" : "slate"} active>{isLlm ? a.provenance : "deterministic"}</Pill>
                      </div>
                      <p className="text-xs leading-relaxed opacity-80">{a.purpose}</p>
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

const DrillLink = ({ to, icon, label }: { to: string; icon: React.ReactNode; label: string }) => (
  <Link
    to={to}
    className="inline-flex items-center gap-2 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:bg-[color:var(--surface-1)] px-3 py-1.5 text-xs uppercase tracking-wider"
  >
    {icon}{label}
  </Link>
);
const Faint = ({ children }: { children: React.ReactNode }) => <div className="text-xs opacity-40">{children}</div>;
const Section = ({ label, icon, children }: { label: string; icon?: React.ReactNode; children: React.ReactNode }) => (
  <div>
    <div className="text-[10px] uppercase tracking-[0.18em] text-[color:var(--text-label)] mb-1.5 flex items-center gap-1">{icon}{label}</div>
    {children}
  </div>
);
const Edge = ({ file, w }: { file: string; w: number }) => (
  <div className="flex items-baseline gap-2 text-xs">
    <span className="font-mono truncate flex-1" title={file}>{base(file)}</span>
    <span className="opacity-40 tabular-nums">x{w}</span>
  </div>
);
