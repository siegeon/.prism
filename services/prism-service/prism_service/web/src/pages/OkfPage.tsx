import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import {
  Page, Card, Kpi, SectionLabel, Pill, Empty, type PillTone,
} from "@/components/ui";
import Markdown from "@/components/Markdown";

// OKF (Open Knowledge Format) wiki — PRISM's memory + brain stores projected
// as a live, read-only OKF bundle (services/okf_host.py). Each concept is a
// frontmatter doc; this page lists them as type-colored cards, expandable to
// fetch the conformant body on demand (progressive disclosure). No raw JSON.

type ConceptMeta = {
  path: string;
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

// OKF concept `type` is free-form (memory type or brain domain). Map the known
// ones to Hermes accent tones; everything else hashes to a stable tone so the
// card field still reads as a color-coded legend.
const TYPE_TONE: Record<string, PillTone> = {
  convention: "sage",
  decision: "amber",
  expertise: "teal",
  "anti-pattern": "rose",
  failure: "rose",
  note: "slate",
  pattern: "teal",
  code: "violet",
};

const HASH_TONES: PillTone[] = ["teal", "sage", "amber", "rose", "violet", "emerald"];
function typeTone(type: string): PillTone {
  const t = (type || "").toLowerCase();
  if (TYPE_TONE[t]) return TYPE_TONE[t];
  let h = 0;
  for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) >>> 0;
  return HASH_TONES[h % HASH_TONES.length];
}

export default function OkfPage() {
  const [project] = useProject();
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
        PRISM's knowledge as a live, read-only{" "}
        <a href="https://openknowledgeformat.org" target="_blank" rel="noreferrer" className="opacity-80 underline">OKF</a>{" "}
        wiki — memory entries and indexed brain docs projected into conformant
        concept documents. Click any card to read its body; nothing here writes
        back to the stores. Also served over MCP (<code className="opacity-80">okf_index</code>,{" "}
        <code className="opacity-80">okf_get</code>) and <code className="opacity-80">/api/okf/raw/index.md</code>.
      </p>

      <section className="flex flex-wrap gap-3">
        <Kpi label="OKF version" value={index?.okf_version ?? "…"} />
        <Kpi label="Concepts" value={index?.concept_count ?? 0} />
        <Kpi label="Sections" value={sections.length} />
      </section>

      <Card>
        <SectionLabel>Section</SectionLabel>
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
        <Card><Empty>No OKF concepts projected for this project yet.</Empty></Card>
      ) : (
        grouped.map((g) => (
          <Card key={g.section}>
            <div className="flex items-baseline gap-3 mb-3">
              <span className="text-[11px] uppercase tracking-wider font-mono opacity-80">{g.section}</span>
              <span className="text-[11px] opacity-50 font-mono">
                {g.items.length} concept{g.items.length === 1 ? "" : "s"}
              </span>
            </div>
            <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3">
              {g.items.map((c) => (
                <ConceptCard key={c.path} meta={c} project={project} />
              ))}
            </div>
          </Card>
        ))
      )}
    </Page>
  );
}

// ConceptCard — type-colored frontmatter card. Collapsed: title + type pill +
// description. Expand fetches the conformant body once (progressive disclosure)
// and renders it with the shared Hermes Markdown renderer — never raw <pre>.
function ConceptCard({ meta, project }: { meta: ConceptMeta; project: string }) {
  const [open, setOpen] = useState(false);
  const [concept, setConcept] = useState<Concept | null>(null);
  const [busy, setBusy] = useState(false);
  const tone = typeTone(meta.type);

  const toggle = () => {
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
      <button onClick={toggle} className="text-left flex flex-col gap-2">
        <div className="flex items-start gap-2">
          <span className="text-[13px] leading-snug font-medium flex-1 min-w-0 text-[color:var(--text-primary)]">
            {meta.title}
          </span>
          {meta.type && (
            <span
              className="text-[10px] uppercase tracking-wider font-mono px-1.5 py-0.5 rounded ring-1 shrink-0"
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
        <div className="text-[10px] font-mono text-[color:var(--text-muted)] truncate">{meta.path}</div>
      </button>

      {open && (
        <div className="mt-1 border-t border-[color:var(--border-default)] pt-2">
          {busy ? (
            <div className="text-[12px] opacity-50">Loading concept…</div>
          ) : concept ? (
            <>
              <Markdown text={concept.body} className="space-y-3" />
              {concept.links.length > 0 && (
                <div className="mt-3 text-[10px] uppercase tracking-wider text-[color:var(--text-label)]">
                  {concept.links.length} link{concept.links.length === 1 ? "" : "s"}
                </div>
              )}
            </>
          ) : (
            <div className="text-[12px] text-[color:var(--accent-rose-fg)]">Failed to load concept body.</div>
          )}
        </div>
      )}
    </div>
  );
}
