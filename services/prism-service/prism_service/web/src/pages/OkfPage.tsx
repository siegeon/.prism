import { useEffect, useMemo, useState, type MouseEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import {
  Page, Card, Kpi, SectionLabel, Pill, Empty,
} from "@/components/ui";
import { typeToneHashed } from "@/lib/domainTone";
import Markdown from "@/components/Markdown";

// OKF (Open Knowledge Format) wiki — PRISM's curated MEMORY expressed as a
// navigable bundle (services/okf_host.py). Memory, OKF, and Understand are the
// SAME knowledge: every concept here is a real memory entry, clicking a card
// opens its real /memory/<id> detail page, [[wikilinks]] navigate to the target
// entry's /memory/<id> page, and code references open the /understand graph (the
// visual of the same connections). Read-only — nothing here writes the stores.

type ConceptMeta = {
  path: string;
  id: string;
  section: string;
  type: string;
  title: string;
  description: string;
};

type OkfIndex = {
  okf_version: string;
  sections: string[];
  concept_count: number;
  paths: string[];
  concepts: ConceptMeta[];
};

type Concept = {
  path: string;
  type: string;
  frontmatter: Record<string, unknown>;
  body: string;
  links: string[];
};

// OKF concept `type` is a memory type. Map the known ones to Hermes accent
// tones; everything else hashes to a stable tone so the card field still reads
// as a color-coded legend.
export default function OkfPage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const [index, setIndex] = useState<OkfIndex | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [section, setSection] = useState<string>("all");

  useEffect(() => {
    setLoaded(false);
    api.get<OkfIndex>(`/api/okf/index?project=${encodeURIComponent(project)}`)
      .then((d) => { setIndex(d); setLoaded(true); })
      .catch(() => { setIndex(null); setLoaded(true); });
  }, [project]);

  const sections = index?.sections ?? [];
  const grouped = useMemo<{ section: string; items: ConceptMeta[] }[]>(() => {
    const concepts = index?.concepts ?? [];
    const buckets: Record<string, ConceptMeta[]> = {};
    for (const c of concepts) {
      if (section !== "all" && c.section !== section) continue;
      (buckets[c.section] ??= []).push(c);
    }
    return Object.keys(buckets)
      .sort()
      .map((s) => ({ section: s, items: buckets[s].sort((a, b) => a.title.localeCompare(b.title)) }));
  }, [index, section]);

  return (
    <Page>
      <p className="text-sm opacity-60">
        PRISM's curated{" "}
        <button onClick={() => navigate("/memory")} className="opacity-80 underline">memory</button>{" "}
        as a navigable{" "}
        <a href="https://openknowledgeformat.org" target="_blank" rel="noreferrer" className="opacity-80 underline">OKF</a>{" "}
        wiki — memory, OKF, and{" "}
        <button onClick={() => navigate("/understand")} className="opacity-80 underline">understand</button>{" "}
        are the same knowledge. Every card is a real memory entry: click it to
        open its detail page, follow a <code className="opacity-80">[[wikilink]]</code> to
        the entry it references, and code citations open the understand graph (the
        visual of the same connections). Read-only — also served over MCP
        (<code className="opacity-80">okf_index</code>,{" "}
        <code className="opacity-80">okf_get</code>) and{" "}
        <code className="opacity-80">/api/okf/raw/index.md</code>.
      </p>

      <section className="flex flex-wrap gap-3">
        <Kpi label="OKF version" value={index?.okf_version ?? "…"} />
        <Kpi label="Concepts" value={index?.concept_count ?? 0} />
        <Kpi label="Domains" value={sections.length} />
      </section>

      <Card>
        <SectionLabel>Domain</SectionLabel>
        <div className="flex flex-wrap gap-2">
          <Pill active={section === "all"} onClick={() => setSection("all")} tone="slate">all</Pill>
          {sections.map((s) => (
            <Pill key={s} active={section === s} onClick={() => setSection(s)} tone="teal">{s}</Pill>
          ))}
        </div>
      </Card>

      {!loaded ? (
        <Card><Empty>Loading OKF bundle…</Empty></Card>
      ) : !index || index.concept_count === 0 ? (
        <Card><Empty>No memory entries projected for this project yet.</Empty></Card>
      ) : (
        grouped.map((g) => (
          <Card key={g.section}>
            <div className="flex items-baseline gap-3 mb-3">
              <span className="text-2xs uppercase tracking-wider font-mono opacity-80">{g.section}</span>
              <span className="text-2xs opacity-50 font-mono">
                {g.items.length} concept{g.items.length === 1 ? "" : "s"}
              </span>
            </div>
            <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3">
              {g.items.map((c) => (
                <ConceptCard key={c.path} meta={c} project={project} navigate={navigate} />
              ))}
            </div>
          </Card>
        ))
      )}
    </Page>
  );
}

// ConceptCard — a real memory entry as a type-colored card. The title opens the
// REAL /memory/<id> detail page (woven into the Memory UI, not a silo); a peek
// toggle expands the conformant body inline (progressive disclosure), rendered
// with the shared Hermes Markdown renderer. Internal links inside the body —
// [[wikilink]] -> /memory/<id> and code refs -> /understand — are intercepted to
// use react-router navigate so they actually navigate (no full page reload).
function ConceptCard({
  meta, project, navigate,
}: {
  meta: ConceptMeta;
  project: string;
  navigate: (to: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [concept, setConcept] = useState<Concept | null>(null);
  const [busy, setBusy] = useState(false);
  const tone = typeToneHashed(meta.type);

  const openDetail = () => {
    if (meta.id) navigate(`/memory/${meta.id}`);
  };

  const togglePeek = () => {
    const next = !open;
    setOpen(next);
    if (next && !concept && !busy) {
      setBusy(true);
      api.get<Concept>(`/api/okf/concept?project=${encodeURIComponent(project)}&path=${encodeURIComponent(meta.path)}`)
        .then((c) => setConcept(c))
        .catch(() => setConcept(null))
        .finally(() => setBusy(false));
    }
  };

  return (
    <div
      className="rounded-md border bg-[color:var(--surface-2)] p-3 flex flex-col gap-2"
      style={{ borderColor: open ? `var(--accent-${tone}-ring)` : "var(--border-default)" }}
    >
      <button onClick={openDetail} className="text-left flex flex-col gap-2" title={`Open /memory/${meta.id}`}>
        <div className="flex items-start gap-2">
          <span className="text-[13px] leading-snug font-medium flex-1 min-w-0 text-[color:var(--text-primary)] underline decoration-dotted decoration-[color:var(--border-strong)] underline-offset-2">
            {meta.title}
          </span>
          {meta.type && (
            <span
              className="text-2xs uppercase tracking-wider font-mono px-1.5 py-0.5 rounded ring-1 shrink-0"
              style={{
                background: `var(--accent-${tone}-bg)`,
                color: `var(--accent-${tone}-fg)`,
                boxShadow: `inset 0 0 0 1px var(--accent-${tone}-ring)`,
              }}
            >
              {meta.type}
            </span>
          )}
        </div>
        {meta.description && (
          <div className="text-[12px] leading-relaxed line-clamp-2 text-[color:var(--text-secondary)]">
            {meta.description}
          </div>
        )}
      </button>

      <div className="flex items-center gap-3 text-2xs uppercase tracking-wider font-mono">
        <button onClick={togglePeek} className="opacity-60 hover:opacity-100">
          {open ? "hide" : "peek"}
        </button>
        <button onClick={openDetail} className="opacity-60 hover:opacity-100">open ↗</button>
        <a
          href={`/api/okf/raw/${meta.path.replace(/^\//, "")}?project=${encodeURIComponent(project)}`}
          target="_blank"
          rel="noreferrer"
          className="opacity-60 hover:opacity-100 ml-auto"
        >
          raw .md
        </a>
      </div>

      {open && (
        <div className="mt-1 border-t border-[color:var(--border-default)] pt-2">
          {busy ? (
            <div className="text-[12px] opacity-50">Loading concept…</div>
          ) : concept ? (
            <WovenMarkdown text={concept.body} navigate={navigate} />
          ) : (
            <div className="text-[12px] text-[color:var(--accent-rose-fg)]">Failed to load concept body.</div>
          )}
        </div>
      )}
    </div>
  );
}

// WovenMarkdown — render a concept body with the shared Markdown component, then
// intercept clicks on internal links (/memory/<id>, /understand) so they route
// via react-router instead of a full page reload. Capture-phase listener on the
// wrapper keeps the shared renderer untouched (it has no link affordance of its
// own) while making [[wikilinks]] and code refs genuinely navigable.
function WovenMarkdown({ text, navigate }: { text: string; navigate: (to: string) => void }) {
  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const a = (e.target as HTMLElement).closest("a");
    if (!a) return;
    const href = a.getAttribute("href") ?? "";
    if (href.startsWith("/memory/") || href.startsWith("/understand")) {
      e.preventDefault();
      navigate(href);
    }
  };
  return (
    <div onClickCapture={onClick}>
      <Markdown text={text} className="space-y-3" />
    </div>
  );
}
