import { useEffect, useMemo, useState, type MouseEvent } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Background, BackgroundVariant, Controls, MiniMap,
  ReactFlow, ReactFlowProvider, useEdgesState, useNodesState,
  type Edge as RfEdge, type Node as RfNode,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Card, Empty, Pill, type PillTone } from "@/components/ui";
import Markdown from "@/components/Markdown";

// Unified Understand wiki (task 89a1ddef) — PRISM's knowledge as ONE
// interconnected wiki: memory + OKF + understanding, graph + read + links.
// Modeled on Google's OKF visualizer: a force-ish concept graph (left) where
// nodes are memory concepts colored by type and edges are authored cross-links,
// plus a detail panel (right) that reads the selected concept, rewires internal
// [[/memory/<id>]] links to SELECT a node in-place (no route change), shows
// 'Cited by' backlinks, and folds in memory edit/retire/supersede. Read-only
// projection over /api/okf/* — never writes brain.db/graph.db.

type GraphNode = {
  id: string; title: string; type: string;
  section: string; path: string; description: string;
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

// Concept node — a memory concept colored by type. Clicking selects it. The
// selected node gets a bright ring so the panel<->graph stay visually linked.
type ConceptNodeData = {
  title: string; type: string; selected: boolean;
};
function ConceptNode({ data }: { data: ConceptNodeData }) {
  const tone = typeTone(data.type);
  return (
    <div
      className="rounded-md border px-2.5 py-1.5 max-w-[180px] cursor-pointer transition-shadow"
      style={{
        background: toneVar(tone, "bg"),
        borderColor: data.selected ? "var(--text-primary)" : toneVar(tone, "ring"),
        boxShadow: data.selected ? "0 0 0 2px var(--text-primary)" : "none",
      }}
      title={data.title}
    >
      <div className="text-[11px] leading-snug font-medium truncate" style={{ color: toneVar(tone, "fg") }}>
        {data.title}
      </div>
      <div className="text-[9px] uppercase tracking-wider font-mono opacity-70" style={{ color: toneVar(tone, "fg") }}>
        {data.type}
      </div>
    </div>
  );
}
const NODE_TYPES = { concept: ConceptNode };

// Clustered layout: group nodes by type into vertical columns (a simple,
// readable stand-in for a force layout — ~112 nodes stay legible, no extra
// heavy dependency). Within a column, lay out in a wrapped grid.
function layout(nodes: GraphNode[], selectedId: string | null): RfNode<ConceptNodeData>[] {
  const byType = new Map<string, GraphNode[]>();
  for (const n of nodes) {
    const t = (n.type || "note").toLowerCase();
    (byType.get(t) ?? byType.set(t, []).get(t)!).push(n);
  }
  const COL_W = 230, ROW_H = 64, GROUP_GAP = 60;
  const out: RfNode<ConceptNodeData>[] = [];
  let x = 0;
  for (const t of Array.from(byType.keys()).sort()) {
    const items = byType.get(t)!;
    const cols = Math.max(1, Math.ceil(Math.sqrt(items.length)));
    items.forEach((n, i) => {
      out.push({
        id: n.id,
        type: "concept",
        position: { x: x + (i % cols) * COL_W, y: Math.floor(i / cols) * ROW_H },
        data: { title: n.title, type: n.type, selected: n.id === selectedId },
      });
    });
    x += cols * COL_W + GROUP_GAP;
  }
  return out;
}

export default function UnderstandPage() {
  const [project] = useProject();
  const [params, setParams] = useSearchParams();
  const [graph, setGraph] = useState<OkfGraph | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<string>("all");
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

  const types = useMemo(() => {
    const s = new Set<string>();
    for (const n of graph?.nodes ?? []) if (n.type) s.add(n.type);
    return Array.from(s).sort();
  }, [graph]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (graph?.nodes ?? []).filter((n) => {
      if (typeFilter !== "all" && n.type !== typeFilter) return false;
      if (!q) return true;
      return (
        n.title.toLowerCase().includes(q) ||
        n.type.toLowerCase().includes(q) ||
        n.section.toLowerCase().includes(q)
      );
    });
  }, [graph, query, typeFilter]);

  const byId = useMemo(() => {
    const m = new Map<string, GraphNode>();
    for (const n of graph?.nodes ?? []) m.set(n.id, n);
    return m;
  }, [graph]);

  const selectedPath = selectedId ? byId.get(selectedId)?.path ?? null : null;

  return (
    <div className="p-6 h-full flex flex-col gap-4 min-w-[720px]">
      <p className="text-sm text-[color:var(--text-secondary)]">
        PRISM knowledge as one interconnected wiki: memory + OKF + understanding,
        graph + read + links.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search concepts by title, type, or domain…"
          className="flex-1 min-w-[220px] text-[13px] rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)] px-3 py-1.5"
        />
        <div className="flex flex-wrap gap-1.5">
          <Pill active={typeFilter === "all"} onClick={() => setTypeFilter("all")} tone="slate">all</Pill>
          {types.map((t) => (
            <Pill key={t} active={typeFilter === t} onClick={() => setTypeFilter(t)} tone={typeTone(t)}>{t}</Pill>
          ))}
        </div>
      </div>

      <div className="flex-1 min-h-[520px] grid grid-cols-[3fr_2fr] gap-4">
        <Card className="p-0 overflow-hidden relative">
          {!loaded ? (
            <div className="p-6"><Empty>Loading concept graph…</Empty></div>
          ) : !graph || graph.nodes.length === 0 ? (
            <div className="p-6"><Empty>No memory concepts projected for this project yet.</Empty></div>
          ) : (
            <ReactFlowProvider>
              <GraphView
                nodes={visible}
                edges={graph.edges}
                selectedId={selectedId}
                onSelect={select}
              />
            </ReactFlowProvider>
          )}
        </Card>

        <Card className="overflow-y-auto">
          <DetailPanel
            path={selectedPath}
            project={project}
            onSelect={select}
            onMutate={() => setBump((b) => b + 1)}
          />
        </Card>
      </div>
    </div>
  );
}

function GraphView({
  nodes, edges, selectedId, onSelect,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const initial = useMemo(() => layout(nodes, selectedId), [nodes, selectedId]);
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState(initial);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<RfEdge>([]);

  useEffect(() => { setRfNodes(layout(nodes, selectedId)); }, [nodes, selectedId, setRfNodes]);
  useEffect(() => {
    const present = new Set(nodes.map((n) => n.id));
    setRfEdges(
      edges
        .filter((e) => present.has(e.source) && present.has(e.target))
        .map((e) => ({
          id: `${e.source}->${e.target}`,
          source: e.source,
          target: e.target,
          style: { stroke: "var(--border-strong)", strokeWidth: 1 },
        })),
    );
  }, [edges, nodes, setRfEdges]);

  return (
    <div className="h-full w-full min-h-[520px]">
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={NODE_TYPES}
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
