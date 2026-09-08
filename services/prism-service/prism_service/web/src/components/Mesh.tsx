// ===========================================================================
// Mesh — THE FOCUSED VIEW: one entity at the centre of its own typed ego
// network, with a rail saying what the whole system knows about it.
//
// It lived inside ExplorePage, which made it look like a property of the
// code-explore page. It is not: it draws whatever token you hand it, and the
// thing it is best at is a TASK — the sessions that drove it, the tests that
// pin it, the code it touched, the memory around it (owner: "i love this
// view ... make that view part of the task so we can see what is in the task
// for contents, that is the content explore"). So it lives here and both
// surfaces import the one component rather than keeping copies that drift.
// Backed by GET /api/xref/neighbors.
// ===========================================================================
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { Card, Empty, ErrorBanner, SectionLabel } from "@/components/ui";
import { GlyphIcon, EntityChip, type EntityKind } from "@/components/EntityChip";
import Dossier from "@/components/Dossier";
import { cn } from "@/lib/utils";

const Faint = ({ children }: { children: React.ReactNode }) => (
  <div className="text-xs opacity-40">{children}</div>
);

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
  // Explore applies the ontology (task 139a8131): the o: class this node's
  // entity is typed with (GET /api/xref/neighbors), '' when the ontology
  // graph doesn't know it yet -- rendered honestly as "unclassified", never
  // guessed.
  ontology_class?: string;
};
// The center additionally carries last_motion (task rows only) for the
// Selected card; it never appears on neighbors.
type MeshCenter = MeshNode & { last_motion?: string | null };
// The TRUE mesh: every edge PRISM found among the collected node set (center
// + hop1 + hop2), not just center-touching spokes -- memory<->memory
// wikilinks, code<->code calls, session<->gate, session<->code, plus the
// implicit spokes themselves. `from`/`to` are node tokens (see MeshNode.token).
// ontology_property is the o: relation this edge's label maps to (o:calls,
// o:imports, ... or the honest o:relatesTo catch-all).
type MeshEdge = { from: string; to: string; label: string; ontology_property?: string };
type MeshData = { center: MeshCenter; neighbors: MeshNode[]; edges: MeshEdge[] };
// The 6 canonical node shapes the mesh draws; anything else falls back to a
// neutral dot. Mirrors EntityChip's EntityKind + the --et-* token set.
const MESH_KINDS: EntityKind[] = ["code", "memory", "task", "test", "gate", "session"];
const isMeshKind = (k: string): k is EntityKind => (MESH_KINDS as string[]).includes(k);

// ===========================================================================
// Mesh — the freeform typed ego network. Center + 1-hop radial neighbors,
// shapes/colors per --et-<kind>. Single click = re-center (wander); double
// click = open the entity. Filter chips toggle kinds; legend maps shape→type.
// ===========================================================================
const MESH_W = 1060, MESH_H = 600, MESH_CX = MESH_W / 2, MESH_CY = MESH_H / 2;

const meshFill = (kind: string) => (isMeshKind(kind) ? `var(--et-${kind})` : "var(--text-label)");
const trunc = (s: string, n = 20) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
// Text halo — a surface-colored stroke painted UNDER the glyphs so every
// label stays readable where it crosses edges, hulls, or another label.
const HALO = {
  paintOrder: "stroke", stroke: "var(--surface-2)",
  strokeWidth: 3, strokeLinejoin: "round",
} as const;

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
type Layout = { placed: Placed[]; posByToken: Map<string, { x: number; y: number }>; centerPos: { x: number; y: number } };

// Deterministic 0..1 hash - seeds the initial scatter so the layout is
// reproducible (same neighborhood, same picture) without any RNG.
function hash01(str: string): number {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return ((h >>> 0) % 100000) / 100000;
}

// ORGANIC force-directed layout (the artifact's look): seeded scatter relaxed
// through ~160 iterations of pairwise repulsion, edge springs (the TRUE mesh
// edges - code pulls toward what it calls, concepts toward what they link),
// same-domain concept cohesion (pools the diamonds for their hull), and mild
// centering. Deterministic and bounded; the selected node floats IN the
// fabric near the middle - a doorway, never a pinned hub.
function layoutMesh(centerToken: string, nodes: MeshNode[], edges: MeshEdge[]): Layout {
  type P = { n?: MeshNode; token: string; x: number; y: number; r: number; hop: number };
  // The focused entity is PINNED to the true canvas center — it is the one
  // fixed point the fabric forms around (the user reads "selected = middle").
  const pts: P[] = [{ token: centerToken, x: MESH_CX, y: MESH_CY, r: 13, hop: 0 }];
  const idx = new Map<string, number>([[centerToken, 0]]);
  const dense = nodes.length > 55;
  nodes.forEach((n) => {
    const a = hash01(n.token) * Math.PI * 2;
    const rad = 110 + hash01(n.token + "r") * 150 + (n.hop === 2 ? 55 : 0);
    pts.push({ n, token: n.token, x: MESH_CX + Math.cos(a) * rad, y: MESH_CY + Math.sin(a) * rad, r: n.hop === 2 ? (dense ? 6.5 : 8) : 12, hop: n.hop ?? 1 });
    idx.set(n.token, pts.length - 1);
  });
  // Adaptive spacing (Fruchterman–Reingold's k): the ideal per-node spacing
  // for THIS count on THIS canvas. Repulsion and spring rests scale off it,
  // so 15 nodes spread wide and 120 settle into a dense-but-legible fabric
  // instead of one clump — no curation, the layout absorbs the count.
  const k = Math.sqrt((MESH_W * MESH_H) / Math.max(pts.length, 1));
  const REP = 0.6 * k * k;
  // Spoke floor stays generous: the hop-1 ring is where the reading happens,
  // so even at 100+ nodes the center's neighborhood keeps label room.
  const restSpoke = Math.min(180, Math.max(130, 0.95 * k));
  const restLink = Math.min(130, Math.max(62, 0.68 * k));
  const springs: [number, number][] = [];
  for (const e of edges) {
    const a = idx.get(e.from), b = idx.get(e.to);
    if (a !== undefined && b !== undefined && a !== b) springs.push([a, b]);
  }
  const domGroups = new Map<string, number[]>();
  pts.forEach((p, i) => {
    const d = p.n?.kind === "memory" ? p.n.domain : null;
    if (d) { const g = domGroups.get(d) ?? []; g.push(i); domGroups.set(d, g); }
  });
  const PAD_X = 66, PAD_TOP = 44, PAD_BOTTOM = 56;
  for (let it = 0; it < 160; it++) {
    const cool = 1 - it / 160;
    const fx = new Array(pts.length).fill(0), fy = new Array(pts.length).fill(0);
    for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) {
      let dx = pts[i].x - pts[j].x, dy = pts[i].y - pts[j].y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = hash01(pts[i].token) - 0.5; dy = hash01(pts[j].token) - 0.5; d2 = 1; }
      const d = Math.sqrt(d2), fr = REP / d2;
      fx[i] += (dx / d) * fr; fy[i] += (dy / d) * fr;
      fx[j] -= (dx / d) * fr; fy[j] -= (dy / d) * fr;
    }
    for (const [a, b] of springs) {
      const rest = pts[a].hop === 0 || pts[b].hop === 0 ? restSpoke : restLink;
      const dx = pts[b].x - pts[a].x, dy = pts[b].y - pts[a].y;
      const d = Math.max(Math.hypot(dx, dy), 1);
      const fs = (d - rest) * 0.045;
      fx[a] += (dx / d) * fs; fy[a] += (dy / d) * fs;
      fx[b] -= (dx / d) * fs; fy[b] -= (dy / d) * fs;
    }
    const centroids: { x: number; y: number; g: number[] }[] = [];
    for (const g of domGroups.values()) {
      if (g.length < 2) continue;
      const cx = g.reduce((acc, i) => acc + pts[i].x, 0) / g.length;
      const cy = g.reduce((acc, i) => acc + pts[i].y, 0) / g.length;
      for (const i of g) { fx[i] += (cx - pts[i].x) * 0.07; fy[i] += (cy - pts[i].y) * 0.07; }
      centroids.push({ x: cx, y: cy, g });
    }
    // Domains repel each other as WHOLE groups so two hulls never merge into
    // one mega-blob with colliding captions.
    for (let a = 0; a < centroids.length; a++) for (let b = a + 1; b < centroids.length; b++) {
      const dx = centroids[a].x - centroids[b].x, dy = centroids[a].y - centroids[b].y;
      const d = Math.max(Math.hypot(dx, dy), 1);
      if (d > 240) continue;
      const push = ((240 - d) / d) * 0.12;
      for (const i of centroids[a].g) { fx[i] += dx * push; fy[i] += dy * push; }
      for (const i of centroids[b].g) { fx[i] -= dx * push; fy[i] -= dy * push; }
    }
    for (let i = 1; i < pts.length; i++) {  // i=0 is the pinned center
      fx[i] += (MESH_CX - pts[i].x) * 0.0018; fy[i] += (MESH_CY - pts[i].y) * 0.0018;
      const mag = Math.hypot(fx[i], fy[i]) || 1;
      const step = Math.min(mag, 14 * cool + 2) / mag;
      pts[i].x = Math.min(MESH_W - PAD_X, Math.max(PAD_X, pts[i].x + fx[i] * step));
      pts[i].y = Math.min(MESH_H - PAD_BOTTOM, Math.max(PAD_TOP, pts[i].y + fy[i] * step));
    }
  }
  // Fill pass: stretch the settled fabric OUTWARD FROM THE PINNED CENTER to
  // the padded canvas — per-side, stretch-only, capped — so a dense
  // neighborhood uses the whole stage while the selected entity stays dead
  // center. Deterministic; a tiny 3-node mesh isn't blown apart.
  if (pts.length > 4) {
    const cx = pts[0].x, cy = pts[0].y;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const cap = 2.2;
    const sxL = Math.min(cap, Math.max(1, (cx - PAD_X) / Math.max(cx - minX, 1)));
    const sxR = Math.min(cap, Math.max(1, (MESH_W - PAD_X - cx) / Math.max(maxX - cx, 1)));
    const syT = Math.min(cap, Math.max(1, (cy - PAD_TOP) / Math.max(cy - minY, 1)));
    const syB = Math.min(cap, Math.max(1, (MESH_H - PAD_BOTTOM - cy) / Math.max(maxY - cy, 1)));
    for (const p of pts) {
      p.x = cx + (p.x - cx) * (p.x < cx ? sxL : sxR);
      p.y = cy + (p.y - cy) * (p.y < cy ? syT : syB);
    }
  }
  const placed: Placed[] = pts.slice(1).map((p) => ({ ...(p.n as MeshNode), x: p.x, y: p.y, hop: p.hop, r: p.r }));
  const posByToken = new Map<string, { x: number; y: number }>();
  placed.forEach((p) => posByToken.set(p.token, { x: p.x, y: p.y }));
  return { placed, posByToken, centerPos: { x: pts[0].x, y: pts[0].y } };
}

type Hull = { domain: string; x: number; y: number; w: number; h: number };

// A rounded bounding box per domain that has 2+ FIRST-HOP memory diamonds.
// Padded to clear each diamond plus its label + type sub-caption; drawn behind
// the edges so it reads as a backdrop, per the artifact's hull(). Second-hop
// diamonds are excluded — the hull is the inner ring's Understand grouping.
function meshHulls(placed: Placed[]): Hull[] {
  const byDom = new Map<string, Placed[]>();
  for (const p of placed) {
    if (p.kind !== "memory" || !p.domain) continue;
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

export default function Mesh({ token, project, hops, onFocus, onOpen, onCenter, revision }: {
  token: string; project: string; hops: 1 | 2;
  onFocus: (t: string) => void; onOpen: (href: string) => void;
  onCenter?: (label: string) => void;
  // A caller that already watches this entity live (the task page holds a
  // /sse/tasks subscription) passes a value derived from that stream; the
  // neighbourhood AND the dossier below re-read on every change, so a new
  // session, test or child appears in the drawing as it happens. Callers
  // with no live source omit it and the view loads once, as before.
  revision?: string | number;
}) {
  const [data, setData] = useState<MeshData | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const clickTimer = useRef<number | null>(null);
  // Zoom/pan: the SVG viewBox is the camera. Wheel zooms at the cursor,
  // drag pans, buttons step-zoom; a re-center resets the camera.
  const [view, setView] = useState({ x: 0, y: 0, w: MESH_W, h: MESH_H });
  const svgRef = useRef<SVGSVGElement | null>(null);
  const panRef = useRef<{ cx: number; cy: number; vx: number; vy: number } | null>(null);
  const movedRef = useRef(false);

  const zoomAt = useCallback((factor: number, fx?: number, fy?: number) => {
    setView((v) => {
      const w = Math.min(MESH_W * 1.5, Math.max(MESH_W / 10, v.w * factor));
      const h = w * (MESH_H / MESH_W);
      const px = fx === undefined ? v.x + v.w / 2 : fx;
      const py = fy === undefined ? v.y + v.h / 2 : fy;
      return { x: px - ((px - v.x) / v.w) * w, y: py - ((py - v.y) / v.h) * h, w, h };
    });
  }, []);
  // Client px -> viewBox units (preserveAspectRatio meet: uniform scale, centered).
  const toViewPoint = useCallback((clientX: number, clientY: number) => {
    const el = svgRef.current;
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const v = viewRef.current;
    const scale = Math.min(r.width / v.w, r.height / v.h);
    const ox = (r.width - v.w * scale) / 2, oy = (r.height - v.h * scale) / 2;
    return { x: v.x + (clientX - r.left - ox) / scale, y: v.y + (clientY - r.top - oy) / scale, scale };
  }, []);
  const viewRef = useRef(view);
  viewRef.current = view;
  // Native non-passive wheel listener (React roots register wheel passive,
  // which would let the page scroll under the zoom).
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const pt = toViewPoint(e.clientX, e.clientY);
      zoomAt(e.deltaY > 0 ? 1.18 : 1 / 1.18, pt?.x, pt?.y);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [toViewPoint, zoomAt, data]);

  // Which neighbourhood the drawing on screen belongs to. A change of
  // ENTITY (or reach) is a new drawing and resets the camera and the filter
  // chips; a change of REVISION is the SAME drawing a moment later, so the
  // data is replaced under a camera the reader positioned themselves. Reset
  // on a live refresh and every task update would yank their zoom back to
  // the top and re-show kinds they had just filtered out.
  const drawnFor = useRef("");
  useEffect(() => {
    const identity = `${token} ${project} ${hops}`;
    const isNewDrawing = drawnFor.current !== identity;
    drawnFor.current = identity;
    let alive = true;
    setLoading(true); setErr(null);
    // Take the FULL fan the API offers — the graph holds thousands of nodes
    // and starving the mesh was the old hairball's mistake in reverse. The
    // server's own degree caps (HOP2_PER_NODE/HOP2_TOTAL) are the guardrail.
    const limit = hops === 2 ? 48 : 64;
    api.get<MeshData>(`/api/xref/neighbors?token=${encodeURIComponent(token)}&project=${encodeURIComponent(project)}&limit=${limit}&hops=${hops}`)
      .then((d) => {
        if (!alive) return;
        setData(d);
        if (isNewDrawing) {
          setHidden(new Set());
          setView({ x: 0, y: 0, w: MESH_W, h: MESH_H });
        }
        onCenter?.(d.center.label);
      })
      // A failed LIVE re-read keeps the drawing that is already up; only a
      // new entity has nothing better to show than the error.
      .catch((e) => { if (alive && isNewDrawing) setErr(String(e?.message || e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [token, project, hops, onCenter, revision]);

  const center = data?.center;

  const visible = useMemo(
    () => (data?.neighbors ?? []).filter((n) => !hidden.has(n.kind)),
    [data, hidden]);
  // Group same-domain memory diamonds so they land adjacent on the ring
  // (a prerequisite for a tight domain hull). Memory nodes lead, clustered by
  // domain; every other kind keeps its order behind them.
  const ordered = useMemo(() => {
    // EVERY fetched node draws — the mesh's promise is the graph's real
    // richness; the layout adapts its spacing to the count instead of the
    // count being curated down to fit a fixed layout.
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
  const { placed, centerPos } = useMemo(
    () => layoutMesh(token, ordered, data?.edges ?? []), [token, ordered, data]);
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
    if (center) m.set(center.token, centerPos);
    placed.forEach((p) => m.set(p.token, { x: p.x, y: p.y }));
    return m;
  }, [placed, center, centerPos]);
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
  // Explore applies the ontology (task 139a8131): the legend groups by
  // ontology_class (with counts) once the ontology graph has classified at
  // least one visible node; otherwise it falls back to the plain kind key.
  const ontologyLegend = useMemo(() => {
    const counts = new Map<string, number>();
    const bump = (n?: MeshNode) => {
      if (n?.ontology_class) counts.set(n.ontology_class, (counts.get(n.ontology_class) ?? 0) + 1);
    };
    bump(center);
    visible.forEach(bump);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [center, visible]);

  // Single click re-centers (wander the mesh); double click opens the page.
  // A short timer disambiguates the two without a jarring double-fire. A
  // click at the end of a pan drag is NOT a click — movedRef gates it.
  const onNodeClick = (nb: MeshNode) => {
    if (movedRef.current) return;
    if (clickTimer.current) window.clearTimeout(clickTimer.current);
    clickTimer.current = window.setTimeout(() => onFocus(nb.token), 200);
  };
  const onNodeDbl = (nb: MeshNode) => {
    if (clickTimer.current) { window.clearTimeout(clickTimer.current); clickTimer.current = null; }
    if (nb.href) onOpen(nb.href);
  };
  const onPanDown = (e: React.PointerEvent<SVGSVGElement>) => {
    // NO pointer capture here: capturing on pointerdown retargets pointerup
    // to the svg root and the browser then never delivers `click` to a node
    // <g> — capture starts lazily below, once this is provably a DRAG.
    panRef.current = { cx: e.clientX, cy: e.clientY, vx: view.x, vy: view.y };
    movedRef.current = false;
  };
  const onPanMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const p = panRef.current;
    const el = svgRef.current;
    if (!p || !el) return;
    const dx = e.clientX - p.cx, dy = e.clientY - p.cy;
    if (!movedRef.current && Math.abs(dx) + Math.abs(dy) > 4) {
      movedRef.current = true;
      try { el.setPointerCapture(e.pointerId); } catch { /* capture is best-effort */ }
    }
    if (!movedRef.current) return;
    const r = el.getBoundingClientRect();
    const scale = Math.min(r.width / view.w, r.height / view.h) || 1;
    setView((v) => ({ ...v, x: p.vx - dx / scale, y: p.vy - dy / scale }));
  };
  const onPanUp = () => { panRef.current = null; };
  const toggleKind = (k: string) =>
    setHidden((prev) => {
      const n = new Set(prev);
      if (n.has(k)) n.delete(k); else n.add(k);
      return n;
    });
  // Dense fabric (2-hop on a rich focus): edge labels stay on the spokes
  // only and hop-2 captions shrink, so 100+ nodes read as texture, not soup.
  const dense = placed.length > 55;

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
            {1 + placed.length} nodes · {renderEdges.length} edges · {domainCount} domain{domainCount === 1 ? "" : "s"}
            {center ? <> · selected <span className="text-[color:var(--text-primary)]">{trunc(center.label, 22)}</span></> : null}
          </span>
        </div>

        <div className="relative flex-1 min-h-0 flex items-center justify-center overflow-hidden p-2">
          {loading && !data ? (
            <div className="p-6 text-xs opacity-60">Loading neighborhood…</div>
          ) : err ? (
            <div className="p-4"><ErrorBanner>{err}</ErrorBanner></div>
          ) : !center ? (
            <div className="p-6"><Empty>Nothing to center on.</Empty></div>
          ) : (
            <>
            <div className="absolute right-3 top-3 z-10 flex flex-col gap-1">
              {([["+", () => zoomAt(1 / 1.35)], ["−", () => zoomAt(1.35)],
                 ["⟲", () => setView({ x: 0, y: 0, w: MESH_W, h: MESH_H })]] as const
              ).map(([lbl, fn]) => (
                <button key={lbl} onClick={fn} aria-label={lbl === "⟲" ? "reset zoom" : lbl === "+" ? "zoom in" : "zoom out"}
                  className="w-7 h-7 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] text-sm leading-none text-[color:var(--text-secondary)] hover:text-[color:var(--text-primary)] hover:border-[color:var(--border-strong)] transition-colors">
                  {lbl}
                </button>
              ))}
            </div>
            <svg ref={svgRef} viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
              preserveAspectRatio="xMidYMid meet"
              className="w-full h-full touch-none select-none"
              style={{ cursor: panRef.current ? "grabbing" : "grab" }} role="img"
              onPointerDown={onPanDown} onPointerMove={onPanMove}
              onPointerUp={onPanUp} onPointerLeave={onPanUp}
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
                // Explore applies the ontology (task 139a8131): the on-canvas
                // label reads the o: property (o:calls, o:relatesTo, ...);
                // the tooltip keeps the original human relation for context.
                const prop = e.ontology_property;
                return (
                  <g key={`${e.from}->${e.to}`}>
                    <title>{prop ? `${e.label} · o:${prop}` : e.label}</title>
                    <line x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                      stroke={spoke ? "var(--border-strong)" : "var(--border-default)"}
                      strokeWidth={spoke ? 1.1 : 0.9} opacity={spoke ? 1 : 0.6} />
                    {(spoke || renderEdges.length <= 28) && (
                      <text {...HALO} x={mx} y={my - 3} textAnchor="middle" fontSize={spoke ? 11 : 10}
                        fill="var(--text-label)" opacity={spoke ? 1 : 0.85}>{prop ?? e.label}</text>
                    )}
                  </g>
                );
              })}
              {/* center — dashed halo marks selection; a doorway, not a hub */}
              <circle cx={centerPos.x} cy={centerPos.y} r={23} fill="none"
                stroke={meshFill(center.kind)} strokeWidth={1.5}
                strokeDasharray="3 3" opacity={0.8} />
              <g style={{ cursor: center.href ? "pointer" : "default" }}
                onDoubleClick={() => center.href && onOpen(center.href)}>
                <MeshShape kind={center.kind} x={centerPos.x} y={centerPos.y} r={16} />
                <text {...HALO} x={centerPos.x} y={centerPos.y + 34} textAnchor="middle" fontSize={13}
                  fontWeight={650} fill="var(--text-primary)">{trunc(center.label, 26)}</text>
                {center.kind === "memory" && center.concept_type && (
                  <text {...HALO} x={centerPos.x} y={centerPos.y + 48} textAnchor="middle" fontSize={9}
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
                    <text {...HALO} x={nb.x} y={nb.y + (two ? (dense ? 17 : 21) : 26)} textAnchor="middle"
                      fontSize={two ? (dense ? 9 : 10.5) : 12}
                      fill="var(--text-secondary)">{trunc(nb.label, two ? (dense ? 14 : 18) : 22)}</text>
                    {/* concept-type sub-caption under the diamond (decision/
                        convention/expertise/anti-pattern/principle) */}
                    {nb.kind === "memory" && nb.concept_type && !two && (
                      <text {...HALO} x={nb.x} y={nb.y + 38} textAnchor="middle" fontSize={9}
                        letterSpacing="0.06em" fill={meshFill("memory")}
                        style={{ textTransform: "uppercase" }}>
                        {nb.concept_type}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
            </>
          )}
        </div>

        {/* legend -- ontology classes once Explore has classified something
            here, else the plain kind-shape key */}
        <div className="flex flex-wrap gap-3 px-3 py-2 border-t border-[color:var(--border-default)] text-2xs text-[color:var(--text-label)]">
          {ontologyLegend.length > 0 ? (
            ontologyLegend.map(([cls, n]) => (
              <span key={cls} className="ont-node" data-kind="class">
                <i className="ont-glyph" />{cls}
                <span className="text-2xs font-mono tabular-nums opacity-70">{n}</span>
              </span>
            ))
          ) : (
            MESH_KINDS.map((k) => (
              <span key={k} className="inline-flex items-center gap-1 capitalize">
                <GlyphIcon kind={k} size={10} /> {k}
              </span>
            ))
          )}
          <span className="ml-auto">scroll = zoom · drag = pan · click a node to re-center · double-click opens it</span>
        </div>
      </div>

      {/* rail */}
      <div className="hidden lg:flex flex-col gap-3 min-h-0 overflow-y-auto">
        {center && (
          <Card className="!p-4 shrink-0">
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
              {center.ontology_class !== undefined && (
                <div className="flex items-center gap-2">
                  <span className="opacity-50 uppercase tracking-wider w-[72px] shrink-0">Ontology</span>
                  {center.ontology_class ? (
                    <a href={`/ontology?tab=structure&class=${encodeURIComponent(center.ontology_class)}`}
                      onClick={(ev) => { ev.preventDefault(); onOpen(`/ontology?tab=structure&class=${encodeURIComponent(center.ontology_class!)}`); }}
                      className="ont-node" data-kind="class">
                      <i className="ont-glyph" />{center.ontology_class}
                    </a>
                  ) : (
                    <span className="text-[color:var(--text-muted)] italic">unclassified</span>
                  )}
                </div>
              )}
            </div>
            {center.href && (
              <button onClick={() => onOpen(center.href!)}
                className="mt-3 cursor-pointer text-xs text-[color:var(--accent-teal-fg)] hover:underline">
                Open detail →
              </button>
            )}
          </Card>
        )}
        {center && (
          <Dossier token={center.token} project={project} onOpen={onOpen}
            // Only the CENTRE is the thing the caller is watching; a wandered
            // neighbour has no live signal, so it reads once like before.
            revision={center.token === token ? revision : undefined} />
        )}
        <Card className="!p-4 shrink-0">
          <SectionLabel>Connections · {visible.length}</SectionLabel>
          <div className="mt-2 flex flex-col gap-1.5 max-h-64 overflow-y-auto pr-1">
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
        {/* Two explainer cards stood here, arguing this view against a
            version of itself that no longer exists ("the old view drew 500
            code-only nodes in 4 blobs") and restating the product's own
            architecture back at the reader. Both removed: the screen shows
            the graph, it does not describe itself. */}
      </div>
    </div>
  );
}
