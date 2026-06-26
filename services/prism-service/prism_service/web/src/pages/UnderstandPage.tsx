import { useEffect, useMemo, useState, type CSSProperties, type MouseEvent } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Background, BackgroundVariant, Controls, Handle, MiniMap, Position,
  ReactFlow, ReactFlowProvider, useEdgesState, useNodesState,
  type Edge as RfEdge, type Node as RfNode,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY,
} from "d3-force";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Card, Empty, type PillTone } from "@/components/ui";
import { prismDomainColor } from "@/components/understand/PrismDomainNode";
import Markdown from "@/components/Markdown";

// Unified Understand wiki (task 89a1ddef) — PRISM's knowledge as ONE
// interconnected wiki: memory + OKF + understanding, graph + read + links.
// Follows the existing Understand DRILL-DOWN UX (see DomainsView/LayersView):
//   LEVEL 1 — concepts grouped by DOMAIN, rendered as Hermes cluster cards
//             (each shows its concept count). A global search jumps to any
//             matching concept. No type-filter pills.
//   LEVEL 2 — click a domain to drill into THAT domain's force-directed
//             concept graph (visible cross-link edges, degree-sized colored
//             dots, ≤~20 readable nodes) with a breadcrumb (Domains / <domain>)
//             to go back. Clicking a node opens the detail panel (right) that
//             reads the selected concept, rewires internal [[/memory/<id>]]
//             links to SELECT a node in-place, shows 'Cited by' backlinks, and
//             folds in memory edit/retire/supersede. Following a link to a
//             concept in another domain drills into that domain too.
// Read-only projection over /api/okf/* — never writes brain.db/graph.db.

type GraphNode = {
  id: string; title: string; type: string;
  section: string; domain: string; path: string; description: string;
};
type GraphEdge = { source: string; target: string };
type OkfGraph = { nodes: GraphNode[]; edges: GraphEdge[] };

type Concept = {
  path: string; type: string;
  frontmatter: Record<string, unknown>;
  body: string; links: string[];
  backlinks: { id: string; title: string }[];
};

const TYPE_TONE: Record<string, PillTone> = {
  convention: "sage", decision: "amber", expertise: "teal",
  "anti-pattern": "rose", failure: "rose", note: "slate",
  pattern: "teal", feedback: "violet", project: "amber",
  reference: "teal", user: "violet",
};
const HASH_TONES: PillTone[] = ["teal", "sage", "amber", "rose", "violet", "emerald"];
function typeTone(type: string): PillTone {
  const t = (type || "").toLowerCase();
  if (TYPE_TONE[t]) return TYPE_TONE[t];
  let h = 0;
  for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) >>> 0;
  return HASH_TONES[h % HASH_TONES.length];
}
function toneVar(tone: PillTone, slot: "bg" | "fg" | "ring"): string {
  return `var(--accent-${tone}-${slot})`;
}

// Concept node — a memory concept rendered as a compact dot colored by type and
// sized by connectivity (hubs read bigger). Clicking selects it; the selected
// node gets a bright ring and a dimmed (non-neighbor) flag so the panel<->graph
// stay visually linked. Hidden handles, centered on the dot, let edges anchor
// dot-center to dot-center.
type ConceptNodeData = {
  title: string; type: string; selected: boolean; dim: boolean; size: number;
};
const HANDLE_STYLE: CSSProperties = {
  top: "50%", left: "50%", transform: "translate(-50%, -50%)",
  width: 1, height: 1, minWidth: 0, minHeight: 0,
  opacity: 0, border: "none", background: "transparent", pointerEvents: "none",
};
function ConceptNode({ data }: { data: ConceptNodeData }) {
  const tone = typeTone(data.type);
  const s = data.size;
  return (
    <div
      className="relative cursor-pointer transition-opacity"
      style={{ width: s, height: s, opacity: data.dim ? 0.3 : 1 }}
      title={`${data.title} · ${data.type}`}
    >
      <Handle type="target" position={Position.Top} style={HANDLE_STYLE} isConnectable={false} />
      <Handle type="source" position={Position.Bottom} style={HANDLE_STYLE} isConnectable={false} />
      <div
        className="rounded-full w-full h-full border transition-all"
        style={{
          background: toneVar(tone, "fg"),
          borderColor: data.selected ? "var(--text-primary)" : toneVar(tone, "ring"),
          borderWidth: data.selected ? 3 : 1.5,
          boxShadow: data.selected
            ? "0 0 0 4px var(--text-primary)"
            : `0 0 0 1px ${toneVar(tone, "bg")}`,
        }}
      />
      <div
        className="absolute left-1/2 top-full mt-1 -translate-x-1/2 text-[10px] leading-tight text-center max-w-[140px] truncate font-medium pointer-events-none"
        style={{ color: data.selected ? "var(--text-primary)" : "var(--text-secondary)" }}
      >
        {data.title}
      </div>
    </div>
  );
}
const NODE_TYPES = { concept: ConceptNode };

type Layout = {
  pos: Map<string, { x: number; y: number }>;
  degree: Map<string, number>;
  maxDegree: number;
};

// Force-directed layout (d3-force): repulsion + edge springs + collision so the
// 86 authored cross-links pull related concepts together and hubs separate from
// the periphery. Computed once over the FULL graph (~113 nodes, 350 ticks ≈ a
// few ms) so positions stay stable when the user searches/filters.
function computeLayout(nodes: GraphNode[], edges: GraphEdge[]): Layout {
  const ids = new Set(nodes.map((n) => n.id));
  const degree = new Map<string, number>();
  for (const id of ids) degree.set(id, 0);
  type SimNode = { id: string; x?: number; y?: number };
  type SimLink = { source: string; target: string };
  const links: SimLink[] = [];
  for (const e of edges) {
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    links.push({ source: e.source, target: e.target });
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
    degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
  }
  let maxDegree = 1;
  for (const d of degree.values()) if (d > maxDegree) maxDegree = d;
  const simNodes: SimNode[] = nodes.map((n) => ({ id: n.id }));
  const sim = forceSimulation<SimNode>(simNodes)
    .force("charge", forceManyBody<SimNode>().strength(-260))
    .force("link", forceLink<SimNode, SimLink>(links).id((d) => d.id).distance(95).strength(0.6))
    .force("center", forceCenter(0, 0))
    .force("collide", forceCollide<SimNode>().radius(34))
    .force("x", forceX(0).strength(0.04))
    .force("y", forceY(0).strength(0.04))
    .stop();
  for (let i = 0; i < 350; i++) sim.tick();
  const pos = new Map<string, { x: number; y: number }>();
  for (const n of simNodes) pos.set(n.id, { x: n.x ?? 0, y: n.y ?? 0 });
  return { pos, degree, maxDegree };
}

// Degree-sized dot radius: hubs read larger, leaves stay compact (16px..44px).
function nodeSize(deg: number, maxDeg: number): number {
  const t = Math.sqrt(deg) / Math.sqrt(Math.max(1, maxDeg));
  return Math.round(16 + t * 28);
}

type DomainGroup = { id: string; count: number; samples: string[] };

const DOMAIN_FALLBACK = "general";

export default function UnderstandPage() {
  const [project] = useProject();
  const [params, setParams] = useSearchParams();
  const [graph, setGraph] = useState<OkfGraph | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [query, setQuery] = useState("");
  // Two-level drill: selectedDomain === null is the LEVEL 1 domain overview;
  // a domain id drills into LEVEL 2 (that domain's concept graph + detail).
  const [selectedDomain, setSelectedDomain] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(params.get("concept"));
  const [bump, setBump] = useState(0);

  useEffect(() => {
    setLoaded(false);
    api.get<OkfGraph>(`/api/okf/graph?project=${encodeURIComponent(project)}`)
      .then((g) => { setGraph(g); setLoaded(true); })
      .catch(() => { setGraph(null); setLoaded(true); });
  }, [project, bump]);

  // Keep the ?concept= deep-link in sync with the in-place selection.
  const select = (id: string | null) => {
    setSelectedId(id);
    const next = new URLSearchParams(params);
    if (id) next.set("concept", id); else next.delete("concept");
    setParams(next, { replace: true });
  };

  const byId = useMemo(() => {
    const m = new Map<string, GraphNode>();
    for (const n of graph?.nodes ?? []) m.set(n.id, n);
    return m;
  }, [graph]);

  // Concepts grouped by domain — the LEVEL 1 entry point.
  const domainGroups = useMemo(() => {
    const m = new Map<string, GraphNode[]>();
    for (const n of graph?.nodes ?? []) {
      const d = n.domain || DOMAIN_FALLBACK;
      const arr = m.get(d) ?? [];
      arr.push(n);
      m.set(d, arr);
    }
    return m;
  }, [graph]);

  const domains = useMemo<DomainGroup[]>(
    () => Array.from(domainGroups.entries())
      .map(([id, ns]) => ({ id, count: ns.length, samples: ns.slice(0, 4).map((n) => n.title) }))
      .sort((a, b) => b.count - a.count),
    [domainGroups],
  );

  // Drill into the domain that owns a concept, then select it. Used by node
  // clicks, [[link]] / backlink follows (even cross-domain), and search picks.
  const goToConcept = (id: string | null) => {
    if (!id) { setSelectedDomain(null); select(null); return; }
    const n = byId.get(id);
    if (n) setSelectedDomain(n.domain || DOMAIN_FALLBACK);
    select(id);
  };

  // ?concept= deep-link: once the graph loads, resolve its domain and drill in.
  useEffect(() => {
    if (!graph || !selectedId || selectedDomain) return;
    const n = byId.get(selectedId);
    if (n) setSelectedDomain(n.domain || DOMAIN_FALLBACK);
  }, [graph, selectedId, selectedDomain, byId]);

  // Global search jumps to matching concepts regardless of the current drill.
  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [] as GraphNode[];
    return (graph?.nodes ?? []).filter((n) =>
      n.title.toLowerCase().includes(q) ||
      n.type.toLowerCase().includes(q) ||
      (n.domain || "").toLowerCase().includes(q),
    ).slice(0, 40);
  }, [graph, query]);

  const selectedPath = selectedId ? byId.get(selectedId)?.path ?? null : null;
  const drillNodes = selectedDomain ? domainGroups.get(selectedDomain) ?? [] : [];

  // Per-domain force layout: ≤~20 nodes pack tightly so the drilled graph is
  // readable. computeLayout keeps only edges among the present nodes.
  const drillLayout = useMemo(
    () => computeLayout(drillNodes, graph?.edges ?? []),
    [drillNodes, graph],
  );

  const selectedDomainIdx = domains.findIndex((d) => d.id === selectedDomain);
  const drillColor = prismDomainColor(selectedDomainIdx >= 0 ? selectedDomainIdx : 0);

  const onPickSearch = (id: string) => { setQuery(""); goToConcept(id); };

  return (
    <div className="p-6 h-full flex flex-col gap-4 min-w-[720px]">
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search concepts by title, type, or domain…"
        className="text-[13px] rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)] px-3 py-1.5"
      />

      {query.trim() ? (
        <SearchResults matches={matches} onPick={onPickSearch} />
      ) : !selectedDomain ? (
        <DomainOverview domains={domains} loaded={loaded} onSelect={setSelectedDomain} />
      ) : (
        <div className="flex-1 min-h-[520px] flex flex-col gap-3">
          <DrillCrumb
            domain={selectedDomain}
            count={drillNodes.length}
            color={drillColor.border}
            onBack={() => goToConcept(null)}
          />
          <div className="flex-1 grid grid-cols-[3fr_2fr] gap-4 min-h-0">
            <Card className="p-0 overflow-hidden relative">
              <ReactFlowProvider>
                <GraphView
                  nodes={drillNodes}
                  allEdges={graph?.edges ?? []}
                  pos={drillLayout.pos}
                  degree={drillLayout.degree}
                  maxDegree={drillLayout.maxDegree}
                  selectedId={selectedId}
                  onSelect={goToConcept}
                />
              </ReactFlowProvider>
            </Card>

            <Card className="overflow-y-auto">
              <DetailPanel
                path={selectedPath}
                project={project}
                onSelect={goToConcept}
                onMutate={() => setBump((b) => b + 1)}
              />
            </Card>
          </div>
        </div>
      )}
    </div>
  );
}

// LEVEL 1 — domains as Hermes cluster cards (mirrors PrismDomainNode's look and
// the DomainsView overview), each showing its concept count. Click to drill in.
function DomainOverview({
  domains, loaded, onSelect,
}: { domains: DomainGroup[]; loaded: boolean; onSelect: (id: string) => void }) {
  if (!loaded) return <Card className="p-6"><Empty>Loading concepts…</Empty></Card>;
  if (domains.length === 0) {
    return <Card className="p-6"><Empty>No memory concepts projected for this project yet.</Empty></Card>;
  }
  const total = domains.reduce((a, d) => a + d.count, 0);
  return (
    <div className="flex-1 min-h-0 overflow-y-auto space-y-4">
      <div className="text-[12px] text-[color:var(--text-secondary)]">
        {domains.length} domain{domains.length === 1 ? "" : "s"} · {total} concept{total === 1 ? "" : "s"}.
        {" "}Click a domain to explore its concept graph.
      </div>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-3">
        {domains.map((d, i) => {
          const c = prismDomainColor(i);
          return (
            <button
              key={d.id}
              onClick={() => onSelect(d.id)}
              className="text-left rounded-xl border-2 px-5 py-4 transition-all hover:brightness-110"
              style={{ borderColor: c.border, background: c.bg }}
            >
              <div className="text-[9px] uppercase tracking-[0.18em] opacity-60 mb-1">Domain</div>
              <div className="font-serif text-base tracking-tight capitalize">{d.id}</div>
              <div className="mt-3 flex flex-wrap gap-1">
                {d.samples.map((s) => (
                  <span key={s} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[color:var(--background-base)]/40 truncate max-w-[180px]">
                    {s}
                  </span>
                ))}
              </div>
              <div className="text-[10px] opacity-60 mt-3 inline-flex items-center gap-1">
                <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: c.dot }} />
                {d.count} concept{d.count === 1 ? "" : "s"}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Global search results — picking one jumps into its domain and selects it.
function SearchResults({
  matches, onPick,
}: { matches: GraphNode[]; onPick: (id: string) => void }) {
  return (
    <Card className="flex-1 min-h-0 overflow-y-auto p-3">
      {matches.length === 0 ? (
        <Empty>No concepts match.</Empty>
      ) : (
        <ul className="space-y-1">
          {matches.map((n) => (
            <li key={n.id}>
              <button
                onClick={() => onPick(n.id)}
                className="w-full text-left px-3 py-2 rounded-md hover:bg-[color:var(--surface-2)] flex items-center justify-between gap-3"
              >
                <span className="truncate">{n.title}</span>
                <span className="text-[10px] opacity-60 font-mono shrink-0">{(n.domain || DOMAIN_FALLBACK)} · {n.type}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// Breadcrumb (Domains / <domain>) — mirrors DomainsView/LayersView DrillCrumb.
function DrillCrumb({
  domain, count, color, onBack,
}: { domain: string; count: number; color: string; onBack: () => void }) {
  return (
    <div
      className="flex items-center gap-3 text-[12px] px-3 py-2 rounded-md bg-[color:var(--background-base)]/40 border border-[color:var(--midground-base)]/15"
      style={{ borderLeftWidth: 3, borderLeftColor: color }}
    >
      <button onClick={onBack} className="uppercase tracking-wider text-[11px] opacity-70 hover:opacity-100">
        ← Domains
      </button>
      <span className="opacity-40">/</span>
      <span className="font-medium capitalize">{domain}</span>
      <span className="opacity-50">— {count} concept{count === 1 ? "" : "s"}</span>
    </div>
  );
}

function GraphView({
  nodes, allEdges, pos, degree, maxDegree, selectedId, onSelect,
}: {
  nodes: GraphNode[];
  allEdges: GraphEdge[];
  pos: Map<string, { x: number; y: number }>;
  degree: Map<string, number>;
  maxDegree: number;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<RfNode<ConceptNodeData>>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<RfEdge>([]);

  // Neighborhood of the selected node — used to highlight its edges and dim the
  // rest so the selection's local structure pops.
  const neighbors = useMemo(() => {
    const s = new Set<string>();
    if (selectedId) {
      s.add(selectedId);
      for (const e of allEdges) {
        if (e.source === selectedId) s.add(e.target);
        else if (e.target === selectedId) s.add(e.source);
      }
    }
    return s;
  }, [allEdges, selectedId]);

  useEffect(() => {
    setRfNodes(
      nodes.map((n) => {
        const p = pos.get(n.id) ?? { x: 0, y: 0 };
        return {
          id: n.id,
          type: "concept",
          position: { x: p.x, y: p.y },
          data: {
            title: n.title,
            type: n.type,
            selected: n.id === selectedId,
            dim: !!selectedId && !neighbors.has(n.id),
            size: nodeSize(degree.get(n.id) ?? 0, maxDegree),
          },
        };
      }),
    );
  }, [nodes, pos, degree, maxDegree, selectedId, neighbors, setRfNodes]);

  useEffect(() => {
    const present = new Set(nodes.map((n) => n.id));
    setRfEdges(
      allEdges
        .filter((e) => present.has(e.source) && present.has(e.target))
        .map((e) => {
          const incident = !!selectedId && (e.source === selectedId || e.target === selectedId);
          return {
            id: `${e.source}->${e.target}`,
            source: e.source,
            target: e.target,
            type: "straight",
            zIndex: incident ? 10 : 0,
            style: {
              stroke: incident ? "var(--text-primary)" : "var(--border-strong)",
              strokeWidth: incident ? 2 : 1,
              opacity: selectedId ? (incident ? 0.9 : 0.08) : 0.3,
            },
          };
        }),
    );
  }, [allEdges, nodes, selectedId, setRfEdges]);

  return (
    <div className="h-full w-full min-h-[520px]">
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={NODE_TYPES}
        nodeOrigin={[0.5, 0.5]}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, n) => onSelect(n.id)}
        nodesDraggable
        nodesConnectable={false}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        proOptions={{ hideAttribution: true }}
        minZoom={0.1}
        maxZoom={2.5}
      >
        <Background variant={BackgroundVariant.Dots} color="rgba(200,180,140,0.10)" gap={24} size={1} />
        <Controls showInteractive={false} position="bottom-left"
          style={{ background: "rgba(0,0,0,0.65)", border: "1px solid rgba(255,255,255,0.18)", borderRadius: 6 }} />
        <MiniMap pannable zoomable
          nodeColor={(n) => `var(--accent-${typeTone((n.data as ConceptNodeData)?.type || "note")}-fg)`}
          style={{ background: "rgba(0,0,0,0.55)", border: "1px solid rgba(255,255,255,0.15)", borderRadius: 6 }} />
      </ReactFlow>
    </div>
  );
}

function DetailPanel({
  path, project, onSelect, onMutate,
}: {
  path: string | null;
  project: string;
  onSelect: (id: string) => void;
  onMutate?: () => void;
}) {
  const [concept, setConcept] = useState<Concept | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    setConcept(null); setEditing(false); setNote(null);
    if (!path) return;
    setBusy(true);
    api.get<Concept>(`/api/okf/concept?project=${encodeURIComponent(project)}&path=${encodeURIComponent(path)}`)
      .then((c) => setConcept(c))
      .catch(() => setConcept(null))
      .finally(() => setBusy(false));
  }, [path, project]);

  if (!path) {
    return <Empty>Select a concept in the graph to read it, follow its links, and edit it.</Empty>;
  }
  if (busy && !concept) return <Empty>Loading concept…</Empty>;
  if (!concept) return <Empty>Failed to load concept.</Empty>;

  const fm = concept.frontmatter;
  const id = String(fm.id ?? "");
  const title = String(fm.title ?? path);
  const tone = typeTone(concept.type);
  const tags = Array.isArray(fm.tags) ? (fm.tags as unknown[]).map(String) : [];

  const doAction = async (action: "edit" | "retire" | "supersede", payload: Record<string, unknown> = {}) => {
    if (!id) return;
    setActionBusy(true);
    try {
      const r = await fetch(`/api/memory/entry/${encodeURIComponent(id)}/action?project=${project}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, ...payload }),
      });
      const b = await r.json().catch(() => ({}));
      if (!r.ok || b.ok === false) {
        setNote(`${action} failed: ${b.detail ?? r.statusText}`);
      } else {
        setNote(`${action} ok.`);
        setEditing(false);
        onMutate?.();
        if (action === "supersede" && b.entry?.id) onSelect(b.entry.id);
      }
    } catch (e) {
      setNote(`${action} failed: ${(e as Error).message ?? e}`);
    } finally {
      setActionBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <header className="space-y-2">
        <div className="flex items-start gap-2 flex-wrap">
          <h1 className="font-serif text-2xl tracking-tight flex-1 min-w-0 break-words">{title}</h1>
          <span className="text-[10px] uppercase tracking-wider font-mono px-1.5 py-0.5 rounded shrink-0"
            style={{ background: toneVar(tone, "bg"), color: toneVar(tone, "fg"), boxShadow: `inset 0 0 0 1px ${toneVar(tone, "ring")}` }}>
            {concept.type}
          </span>
        </div>
        {tags.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {tags.map((t) => (
              <span key={t} className="text-[10px] uppercase tracking-wider font-mono px-1.5 py-0.5 rounded bg-[color:var(--surface-2)] text-[color:var(--text-muted)]">{t}</span>
            ))}
          </div>
        )}
      </header>

      <div className="flex items-center gap-2">
        {!editing && (
          <button disabled={actionBusy} onClick={() => { setDraft(concept.body); setEditing(true); }}
            className="text-[10px] uppercase tracking-wider px-2 py-1 rounded bg-[color:var(--midground-base)]/15 hover:bg-[color:var(--midground-base)]/30 disabled:opacity-40">Edit</button>
        )}
        <button disabled={actionBusy} onClick={() => doAction("retire")}
          className="text-[10px] uppercase tracking-wider px-2 py-1 rounded disabled:opacity-40"
          style={{ background: "var(--accent-amber-bg)", color: "var(--accent-amber-fg)" }}>Retire</button>
        <a href={`/api/okf/raw/${path.replace(/^\//, "")}?project=${encodeURIComponent(project)}`}
          target="_blank" rel="noreferrer"
          className="text-[10px] uppercase tracking-wider px-2 py-1 rounded ml-auto opacity-60 hover:opacity-100">raw .md</a>
      </div>
      {note && <div className="text-[11px] opacity-70">{note}</div>}

      {editing ? (
        <div className="space-y-2">
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={10}
            className="w-full text-[13px] rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)] p-3 leading-relaxed resize-y font-sans" />
          <div className="flex gap-2">
            <button disabled={actionBusy} onClick={() => doAction("edit", { description: draft })}
              className="text-[10px] uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
              style={{ background: "var(--accent-emerald-bg)", color: "var(--accent-emerald-fg)" }}>Save edit</button>
            <button disabled={actionBusy} onClick={() => doAction("supersede", { description: draft })}
              className="text-[10px] uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
              style={{ background: "var(--accent-violet-bg)", color: "var(--accent-violet-fg)" }}
              title="store a new generation under the same name (archives this one)">Supersede</button>
            <button onClick={() => setEditing(false)}
              className="text-[10px] uppercase tracking-wider px-3 py-1.5 rounded bg-[color:var(--midground-base)]/15">Cancel</button>
          </div>
        </div>
      ) : (
        <WovenMarkdown text={concept.body} onSelect={onSelect} />
      )}

      <div className="border-t border-[color:var(--border-default)] pt-3">
        <div className="text-[10px] uppercase tracking-wider text-[color:var(--text-label)] mb-2">
          Cited by ({concept.backlinks.length})
        </div>
        {concept.backlinks.length === 0 ? (
          <div className="text-[12px] opacity-50">No other concept links here yet.</div>
        ) : (
          <ul className="space-y-1">
            {concept.backlinks.map((b) => (
              <li key={b.id}>
                <button onClick={() => onSelect(b.id)}
                  className="text-[12px] underline decoration-dotted underline-offset-2 text-[color:var(--accent-teal-fg)] hover:opacity-80 text-left">
                  {b.title}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// WovenMarkdown — render the concept body with the shared Hermes Markdown
// component, then intercept internal link clicks. A /memory/<id> link SELECTS
// that node in-place (no route change); /understand code refs navigate normally.
function WovenMarkdown({ text, onSelect }: { text: string; onSelect: (id: string) => void }) {
  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const a = (e.target as HTMLElement).closest("a");
    if (!a) return;
    const href = a.getAttribute("href") ?? "";
    const m = /^\/memory\/([^/]+)/.exec(href);
    if (m) { e.preventDefault(); onSelect(m[1]); }
  };
  return (
    <div onClickCapture={onClick}>
      <Markdown text={text} className="space-y-3" />
    </div>
  );
}
