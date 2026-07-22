import { useEffect, useMemo, useState, type MouseEvent, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Network } from "lucide-react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Card, Empty } from "@/components/ui";
import { typeToneHashed, toneVar, type Tone } from "@/lib/domainTone";
import { Lozenge, type LozengeTone } from "@/components/Lozenge";
import { EntityChip, GlyphIcon } from "@/components/EntityChip";
import Markdown from "@/components/Markdown";

// Understand — the domain-first knowledge drill (owner doctrine): Understand
// starts at the DOMAIN layer and drills DOWN. It never opens on a concept.
//   LEVEL 1 — a grid of domain cards: each shows its concept count and a thin
//             stacked mix bar colored by concept TYPE (the OKF type tones, one
//             source of truth in domainTone). This is the front door.
//   LEVEL 2 — click a domain → that domain's concepts as a scannable table
//             (type Lozenge · concept name · how many concepts cite it).
//   LEVEL 3 — click a concept → the RETAINED reading page (DetailPanel): body
//             markdown with live [[wikilinks]], backlinks, edit/retire/supersede.
// A breadcrumb (Understand / <domain> / <concept>) walks back up every level.
// Deep links keep working: an inbound ?concept=<id> resolves the concept's
// domain and opens the reading page directly — "never open on a concept" is the
// DEFAULT landing rule, not a ban on inbound links.
// Read-only projection over /api/okf/* — never writes brain.db / graph.db.

type GraphNode = {
  id: string; title: string; type: string;
  section: string; domain: string; path: string; description: string;
};
type GraphEdge = { source: string; target: string };
type OkfGraph = { nodes: GraphNode[]; edges: GraphEdge[] };

// Tasks that recalled this concept (from the memory recall_log) — the "Cited
// by" extension beyond concept-to-concept backlinks. Sessions are not
// Sessions became attributable in task fc258f15 (recall_log.session_id).
type Recaller = { task_id: string; recall_count?: number; last_recalled?: string; outcome?: string };
type SessionRecaller = { session_id: string; recall_count?: number; last_recalled?: string };

type Concept = {
  path: string; type: string;
  frontmatter: Record<string, unknown>;
  body: string; links: string[];
  backlinks: { id: string; title: string }[];
  recalled_by?: Recaller[];
  recalled_by_sessions?: SessionRecaller[];
};

const DOMAIN_FALLBACK = "general";

// Concept type → Lozenge tone, routed through the canonical type→Tone map so
// there is ONE source of type color. typeToneHashed yields a Hermes Tone; this
// translates it to the Lozenge vocabulary (the two share the same accent
// families — neutral↔slate, info↔teal, ok↔sage, warn↔amber, danger↔rose,
// new↔violet), so a type's lozenge and its mix-bar segment read the same hue.
const TONE_LZ: Record<Tone, LozengeTone> = {
  slate: "neutral", teal: "info", sage: "ok",
  amber: "warn", rose: "danger", violet: "new", emerald: "ok",
};
function typeLozengeTone(type: string): LozengeTone {
  return TONE_LZ[typeToneHashed(type)];
}

type TypeSlice = { type: string; count: number; tone: Tone };
type DomainCard = { id: string; count: number; mix: TypeSlice[] };

export default function UnderstandPage() {
  const [project] = useProject();
  const [params, setParams] = useSearchParams();
  const [graph, setGraph] = useState<OkfGraph | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [query, setQuery] = useState("");
  // Drill state: selectedId (a concept) is LEVEL 3; else selectedDomain is
  // LEVEL 2; else the domain grid is LEVEL 1. selectedId seeds from ?concept=
  // so an inbound deep link opens the reading page.
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

  // Concepts grouped by domain — the LEVEL 1 spine.
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

  // Domain cards: count + a type histogram (sorted desc) for the mix bar.
  const domains = useMemo<DomainCard[]>(
    () => Array.from(domainGroups.entries())
      .map(([id, ns]) => {
        const counts = new Map<string, number>();
        for (const n of ns) counts.set(n.type, (counts.get(n.type) ?? 0) + 1);
        const mix: TypeSlice[] = Array.from(counts.entries())
          .map(([type, count]) => ({ type, count, tone: typeToneHashed(type) }))
          .sort((a, b) => b.count - a.count);
        return { id, count: ns.length, mix };
      })
      .sort((a, b) => b.count - a.count),
    [domainGroups],
  );

  // "Cited by" = inbound cross-link count (edges whose target is this concept).
  const citedBy = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of graph?.edges ?? []) m.set(e.target, (m.get(e.target) ?? 0) + 1);
    return m;
  }, [graph]);

  // Drill into the domain that owns a concept, then select it. Used by table
  // rows, [[link]] / backlink follows (even cross-domain), and search picks.
  const goToConcept = (id: string | null) => {
    if (!id) { select(null); return; }
    const n = byId.get(id);
    if (n) setSelectedDomain(n.domain || DOMAIN_FALLBACK);
    select(id);
  };

  // ?concept= deep-link: once the graph loads, resolve its domain so the
  // breadcrumb can walk back into the right domain table.
  useEffect(() => {
    if (!graph || !selectedId) return;
    const n = byId.get(selectedId);
    if (n && !selectedDomain) setSelectedDomain(n.domain || DOMAIN_FALLBACK);
  }, [graph, selectedId, selectedDomain, byId]);

  // Global search — an alternate door into the same knowledge, offered on the
  // browsing levels (never on the reading page).
  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [] as GraphNode[];
    return (graph?.nodes ?? []).filter((n) =>
      n.title.toLowerCase().includes(q) ||
      n.type.toLowerCase().includes(q) ||
      (n.domain || "").toLowerCase().includes(q),
    ).slice(0, 40);
  }, [graph, query]);

  const selectedNode = selectedId ? byId.get(selectedId) : undefined;
  const selectedPath = selectedNode?.path ?? null;
  const breadcrumbDomain = selectedNode?.domain || selectedDomain || DOMAIN_FALLBACK;
  const domainNodes = selectedDomain ? domainGroups.get(selectedDomain) ?? [] : [];

  const onPickSearch = (id: string) => { setQuery(""); goToConcept(id); };
  const toDomains = () => { setSelectedDomain(null); select(null); };
  const toDomain = () => select(null);

  // LEVEL 3 — the reading page. Retained wiki surface; only the shell around it
  // (breadcrumb) changed. Deep links land here directly.
  if (selectedId) {
    return (
      <div className="p-6 h-full flex flex-col gap-4 min-w-[720px]">
        <Breadcrumb
          items={[
            { label: "Understand", onClick: toDomains },
            { label: breadcrumbDomain, onClick: toDomain },
            { label: selectedNode?.title ?? selectedId },
          ]}
        />
        <Card className="flex-1 min-h-0 overflow-y-auto max-w-[860px]">
          <DetailPanel
            path={selectedPath}
            project={project}
            onSelect={goToConcept}
            onMutate={() => setBump((b) => b + 1)}
          />
        </Card>
      </div>
    );
  }

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
        <DomainTable
          domain={selectedDomain}
          nodes={domainNodes}
          citedBy={citedBy}
          onBack={toDomains}
          onOpen={goToConcept}
        />
      )}
    </div>
  );
}

// Breadcrumb — Understand / <domain> / <concept>. Every segment except the last
// is a button that walks back up a level.
function Breadcrumb({
  items,
}: { items: { label: string; onClick?: () => void }[] }) {
  return (
    <div className="flex items-center gap-2 text-[12px] text-[color:var(--text-muted)]">
      {items.map((it, i) => (
        <span key={i} className="flex items-center gap-2 min-w-0">
          {i > 0 && <span className="opacity-40">/</span>}
          {it.onClick ? (
            <button onClick={it.onClick} className="capitalize hover:text-[color:var(--text-primary)]">
              {it.label}
            </button>
          ) : (
            <span className="capitalize text-[color:var(--text-secondary)] truncate max-w-[320px]">
              {it.label}
            </span>
          )}
        </span>
      ))}
    </div>
  );
}

// LEVEL 1 — domain cards. Each card: a DOMAIN kicker, the domain name, its
// concept count, and a thin stacked bar whose segments are the domain's concept
// TYPES (colored by the canonical type tone). Click to drill into the domain.
function DomainOverview({
  domains, loaded, onSelect,
}: { domains: DomainCard[]; loaded: boolean; onSelect: (id: string) => void }) {
  if (!loaded) return <Card className="p-6"><Empty>Loading concepts…</Empty></Card>;
  if (domains.length === 0) {
    return <Card className="p-6"><Empty>No memory concepts projected for this project yet.</Empty></Card>;
  }
  const total = domains.reduce((a, d) => a + d.count, 0);
  return (
    <div className="flex-1 min-h-0 overflow-y-auto space-y-4">
      <div className="text-[12px] text-[color:var(--text-secondary)]">
        {domains.length} domain{domains.length === 1 ? "" : "s"} · {total} concept{total === 1 ? "" : "s"}.
        {" "}Start at a domain; the bar shows its mix of concept types.
      </div>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-3">
        {domains.map((d) => (
          <button
            key={d.id}
            onClick={() => onSelect(d.id)}
            className="text-left rounded-lg border px-4 py-3.5 transition-colors
              bg-[color:var(--surface-1)] border-[color:var(--border-default)]
              hover:border-[color:var(--accent-teal-ring)] hover:bg-[color:var(--surface-2)]"
          >
            <div className="text-2xs uppercase tracking-[0.16em] text-[color:var(--text-muted)]">Domain</div>
            <div className="font-serif text-[15px] tracking-tight capitalize mt-0.5 text-[color:var(--text-primary)]">
              {d.id}
            </div>
            <div className="text-[12px] text-[color:var(--text-muted)] mt-0.5">
              {d.count} concept{d.count === 1 ? "" : "s"}
            </div>
            <MixBar mix={d.mix} total={d.count} />
          </button>
        ))}
      </div>
    </div>
  );
}

// The type-mix bar — one segment per concept type, width proportional to count,
// filled with the type's canonical tone (fg = the saturated step, reads solid).
function MixBar({ mix, total }: { mix: TypeSlice[]; total: number }) {
  return (
    <div className="mt-2.5 flex h-[5px] overflow-hidden rounded-[3px] bg-[color:var(--surface-3)]">
      {mix.map((s) => (
        <div
          key={s.type}
          title={`${s.type} · ${s.count}`}
          style={{ width: `${(s.count / Math.max(1, total)) * 100}%`, background: toneVar(s.tone, "fg") }}
        />
      ))}
    </div>
  );
}

// LEVEL 2 — one domain's concepts as a scannable table. Type as a Lozenge, the
// concept name as a summary link (memory glyph + title), and how many concepts
// cite it. Rows sort by citation count so hubs rise. Click a row to read it.
function DomainTable({
  domain, nodes, citedBy, onBack, onOpen,
}: {
  domain: string;
  nodes: GraphNode[];
  citedBy: Map<string, number>;
  onBack: () => void;
  onOpen: (id: string) => void;
}) {
  const rows = useMemo(
    () => [...nodes].sort(
      (a, b) => (citedBy.get(b.id) ?? 0) - (citedBy.get(a.id) ?? 0) || a.title.localeCompare(b.title),
    ),
    [nodes, citedBy],
  );
  return (
    <div className="flex-1 min-h-0 flex flex-col gap-3">
      <Breadcrumb items={[{ label: "Understand", onClick: onBack }, { label: domain }]} />
      <div className="text-[12px] text-[color:var(--text-secondary)]">
        {rows.length} concept{rows.length === 1 ? "" : "s"}. Click one to read it.
      </div>
      <Card className="flex-1 min-h-0 overflow-y-auto p-0">
        <table className="w-full text-[13.5px] border-collapse">
          <thead>
            <tr>
              <Th className="w-[130px]">Type</Th>
              <Th>Concept</Th>
              <Th className="w-[92px] text-right">Cited by</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[color:var(--border-default)]">
            {rows.map((n) => (
              <tr
                key={n.id}
                onClick={() => onOpen(n.id)}
                className="cursor-pointer hover:bg-[color:var(--surface-2)]"
              >
                <td className="px-3 py-2 align-middle">
                  <Lozenge tone={typeLozengeTone(n.type)}>{n.type}</Lozenge>
                </td>
                <td className="px-3 py-2 align-middle">
                  <div className="flex items-center gap-2 min-w-0">
                    <GlyphIcon kind="memory" />
                    <span className="truncate font-medium text-[color:var(--text-primary)]">{n.title}</span>
                  </div>
                </td>
                <td className="px-3 py-2 align-middle text-right tabular-nums text-[color:var(--text-muted)]">
                  {citedBy.get(n.id) ?? 0}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

function Th({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <th className={`text-left text-[11px] uppercase tracking-wide font-semibold px-3 py-2
      text-[color:var(--text-label)] border-b border-[color:var(--border-default)] ${className ?? ""}`}>
      {children}
    </th>
  );
}

// Global search results — picking one jumps into its domain and opens it.
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
                <span className="flex items-center gap-2 min-w-0">
                  <GlyphIcon kind="memory" />
                  <span className="truncate">{n.title}</span>
                </span>
                <span className="text-2xs opacity-60 font-mono shrink-0">{(n.domain || DOMAIN_FALLBACK)} · {n.type}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// LEVEL 3 detail — the RETAINED reading surface (unchanged in role): reads the
// selected concept, rewires internal [[/memory/<id>]] links to drill in-place,
// shows 'Cited by' backlinks, and folds in memory edit/retire/supersede.
function DetailPanel({
  path, project, onSelect, onMutate,
}: {
  path: string | null;
  project: string;
  onSelect: (id: string) => void;
  onMutate?: () => void;
}) {
  const navigate = useNavigate();
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
    return <Empty>Select a concept to read it, follow its links, and edit it.</Empty>;
  }
  if (busy && !concept) return <Empty>Loading concept…</Empty>;
  if (!concept) return <Empty>Failed to load concept.</Empty>;

  const fm = concept.frontmatter;
  const id = String(fm.id ?? "");
  const title = String(fm.title ?? path);
  const tone = typeToneHashed(concept.type);
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
          <h1 className="text-[20px] font-[650] leading-tight tracking-[-0.01em] flex-1 min-w-0 break-words">{title}</h1>
          {/* Two doors: the reading page jumps to this concept AS a mesh node
              (/brain?focus=<id>); the mesh deep-links back here via ?concept=. */}
          {id && (
            <button
              onClick={() => navigate(`/brain?focus=${encodeURIComponent(id)}`)}
              title="Show this concept in the Explore mesh"
              className="shrink-0 inline-flex items-center gap-1 text-2xs uppercase tracking-wider px-2 py-1 rounded
                border border-[color:var(--accent-teal-ring)] text-[color:var(--accent-teal-fg)] hover:bg-[color:var(--surface-2)]">
              <Network className="w-3 h-3" /> Show in mesh
            </button>
          )}
          <span className="text-2xs uppercase tracking-wider font-mono px-1.5 py-0.5 rounded shrink-0"
            style={{ background: toneVar(tone, "bg"), color: toneVar(tone, "fg"), boxShadow: `inset 0 0 0 1px ${toneVar(tone, "ring")}` }}>
            {concept.type}
          </span>
        </div>
        {tags.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {tags.map((t) => (
              <span key={t} className="text-2xs uppercase tracking-wider font-mono px-1.5 py-0.5 rounded bg-[color:var(--surface-2)] text-[color:var(--text-muted)]">{t}</span>
            ))}
          </div>
        )}
      </header>

      <div className="flex items-center gap-2">
        {!editing && (
          <button disabled={actionBusy} onClick={() => { setDraft(concept.body); setEditing(true); }}
            className="text-2xs uppercase tracking-wider px-2 py-1 rounded bg-[color:var(--midground-base)]/15 hover:bg-[color:var(--midground-base)]/30 disabled:opacity-40">Edit</button>
        )}
        <button disabled={actionBusy} onClick={() => doAction("retire")}
          className="text-2xs uppercase tracking-wider px-2 py-1 rounded disabled:opacity-40"
          style={{ background: "var(--accent-amber-bg)", color: "var(--accent-amber-fg)" }}>Retire</button>
        <a href={`/api/okf/raw/${path.replace(/^\//, "")}?project=${encodeURIComponent(project)}`}
          target="_blank" rel="noreferrer"
          className="text-2xs uppercase tracking-wider px-2 py-1 rounded ml-auto opacity-60 hover:opacity-100">raw .md</a>
      </div>
      {note && <div className="text-2xs opacity-70">{note}</div>}

      {editing ? (
        <div className="space-y-2">
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={10}
            className="w-full text-[13px] rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)] p-3 leading-relaxed resize-y font-sans" />
          <div className="flex gap-2">
            <button disabled={actionBusy} onClick={() => doAction("edit", { description: draft })}
              className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
              style={{ background: "var(--accent-emerald-bg)", color: "var(--accent-emerald-fg)" }}>Save edit</button>
            <button disabled={actionBusy} onClick={() => doAction("supersede", { description: draft })}
              className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
              style={{ background: "var(--accent-violet-bg)", color: "var(--accent-violet-fg)" }}
              title="store a new generation under the same name (archives this one)">Supersede</button>
            <button onClick={() => setEditing(false)}
              className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded bg-[color:var(--midground-base)]/15">Cancel</button>
          </div>
        </div>
      ) : (
        <WovenMarkdown text={concept.body} onSelect={onSelect} />
      )}

      <div className="border-t border-[color:var(--border-default)] pt-3">
        <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)] mb-2">
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

      {/* "Cited by" beyond concepts: tasks AND sessions that recalled this
          concept, from the memory recall_log (sessions since fc258f15).
          Guarded — hidden entirely when nothing has. */}
      {(concept.recalled_by?.length ?? 0) > 0 && (
        <div className="border-t border-[color:var(--border-default)] pt-3">
          <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)] mb-2">
            Recalled by tasks ({concept.recalled_by!.length})
          </div>
          <ul className="space-y-1.5">
            {concept.recalled_by!.map((t) => (
              <li key={t.task_id} className="flex items-center gap-2 min-w-0">
                <EntityChip kind="task" label={t.task_id.slice(0, 8)} to={`/tasks/${t.task_id}`} title={t.task_id} />
                {t.outcome ? (
                  <span className="text-2xs text-[color:var(--text-muted)] capitalize">{t.outcome}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(concept.recalled_by_sessions?.length ?? 0) > 0 && (
        <div className="border-t border-[color:var(--border-default)] pt-3">
          <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)] mb-2">
            Recalled in sessions ({concept.recalled_by_sessions!.length})
          </div>
          <ul className="space-y-1.5">
            {concept.recalled_by_sessions!.map((sr) => (
              <li key={sr.session_id} className="flex items-center gap-2 min-w-0">
                <EntityChip kind="session" label={sr.session_id.slice(0, 8)} to={`/sessions/${sr.session_id}`} title={sr.session_id} />
                {(sr.recall_count ?? 0) > 1 ? (
                  <span className="text-2xs text-[color:var(--text-muted)]">× {sr.recall_count}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      )}
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
