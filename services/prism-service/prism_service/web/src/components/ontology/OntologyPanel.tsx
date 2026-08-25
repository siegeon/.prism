import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { Card, Empty } from "@/components/ui";

// Ontology entry point on the Understand page (task 15c06516, owner's top
// priority: "i dont see the ontology stuff"). Subsume-style INDEX RAIL of
// PERSISTED classes (grouped by source: prototype rows vs the code graph),
// numbered, with instance counts — click a class to see its instances
// (round pills, capped "+N more"), properties (chromeless mono), and
// axioms (quiet graphite). Shape carries kind (.ont-node[data-kind]
// primitives in index.css, copied from the fe62a2ee Subsume prototype).
// Read-only projection over /api/okf/ontology* — never computed here.

type OntClass = {
  id: string; name: string; kind: string; parent_id: string | null;
  description: string; instance_count: number; source: string;
};
type OntProperty = {
  id: string; name: string; domain_class: string | null;
  range_class: string | null; kind: string;
};
type OntAxiom = { id: string; name: string; description: string; state: string; detail: string };
type OntInstance = { id: string; class_id: string; label: string; ref: string; provenance: string };
type OntologyPayload = { classes: OntClass[]; properties: OntProperty[]; axioms: OntAxiom[] };

const CODE_GRAPH_SOURCE = "graph";
const INSTANCE_CAP = 24;

export default function OntologyPanel({ project }: { project: string }) {
  const [data, setData] = useState<OntologyPayload | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [instances, setInstances] = useState<OntInstance[]>([]);

  useEffect(() => {
    api.get<OntologyPayload>(`/api/okf/ontology?project=${encodeURIComponent(project)}`)
      .then((d) => { setData(d); setSelected((prev) => prev ?? d.classes[0]?.id ?? null); })
      .catch(() => setData(null));
  }, [project]);

  useEffect(() => {
    if (!selected) { setInstances([]); return; }
    const q = `project=${encodeURIComponent(project)}&class_id=${encodeURIComponent(selected)}&limit=200`;
    api.get<{ instances: OntInstance[] }>(`/api/okf/ontology/instances?${q}`)
      .then((r) => setInstances(r.instances))
      .catch(() => setInstances([]));
  }, [project, selected]);

  const groups = useMemo(() => {
    const prototype: OntClass[] = [];
    const codeGraph: OntClass[] = [];
    for (const c of data?.classes ?? []) {
      (c.source === CODE_GRAPH_SOURCE ? codeGraph : prototype).push(c);
    }
    return { prototype, codeGraph };
  }, [data]);

  if (!data) return <Card className="p-6"><Empty>Loading ontology…</Empty></Card>;
  if (data.classes.length === 0) {
    return <Card className="p-6"><Empty>No ontology classes projected yet.</Empty></Card>;
  }

  const selectedClass = data.classes.find((c) => c.id === selected) ?? null;
  const visible = instances.slice(0, INSTANCE_CAP);
  const overflow = instances.length - visible.length;

  return (
    <div className="flex-1 min-h-0 grid grid-cols-[220px_minmax(0,1fr)] gap-4">
      <nav className="overflow-y-auto border-r border-[color:var(--border-default)] pr-3">
        <ClassGroup title="Prototype" items={groups.prototype} selected={selected} onSelect={setSelected} />
        <ClassGroup title="Code graph" items={groups.codeGraph} selected={selected} onSelect={setSelected} />
      </nav>
      <div className="overflow-y-auto space-y-5">
        {selectedClass && (
          <div>
            {/* Shape carries kind: an ontology_classes row is either a
                concrete 'class' (square) or a never-instantiated 'abstract'
                grouping (hatched) — see OntologyStore's kind column. */}
            {selectedClass.kind === "abstract" ? (
              <span className="ont-node" data-kind="abstract"><i className="ont-glyph" />{selectedClass.name}</span>
            ) : (
              <span className="ont-node" data-kind="class"><i className="ont-glyph" />{selectedClass.name}</span>
            )}
            <div className="text-[12px] text-[color:var(--text-muted)] mt-1">
              {selectedClass.instance_count} instance{selectedClass.instance_count === 1 ? "" : "s"} · {selectedClass.source}
            </div>
          </div>
        )}

        <Section title="Instances">
          {visible.map((i) => (
            <span key={i.id} className="ont-node" data-kind="instance"><i className="ont-glyph" />{i.label}</span>
          ))}
          {overflow > 0 && <span className="ont-rolled">+{overflow} more</span>}
        </Section>

        <Section title="Properties">
          {data.properties.map((p) => (
            p.kind === "literal" ? (
              <span key={p.id} className="ont-node" data-kind="literal">{p.name}</span>
            ) : (
              <span key={p.id} className="ont-node" data-kind="property">{p.name}</span>
            )
          ))}
        </Section>

        <Section title="Axioms">
          {data.axioms.map((a) => (
            <span key={a.id} className="ont-axiom" data-state={a.state}>{a.name}</span>
          ))}
        </Section>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)] mb-2">{title}</div>
      <div className="flex flex-wrap gap-2">{children}</div>
    </div>
  );
}

function ClassGroup({
  title, items, selected, onSelect,
}: { title: string; items: OntClass[]; selected: string | null; onSelect: (id: string) => void }) {
  if (items.length === 0) return null;
  return (
    <div className="mb-4">
      <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)] mb-1.5 border-b border-[color:var(--border-default)] pb-1">
        {title}
      </div>
      <ul className="space-y-0.5">
        {items.map((c, i) => {
          const current = selected === c.id;
          return (
            <li key={c.id}>
              <button
                onClick={() => onSelect(c.id)}
                className={`w-full text-left flex items-center gap-2 px-2 py-1 rounded text-[13px] hover:bg-[color:var(--surface-2)] ${current ? "bg-[color:var(--surface-2)] font-semibold" : ""}`}
              >
                <span className="font-mono text-2xs text-[color:var(--text-muted)]">{String(i + 1).padStart(2, "0")}</span>
                <span className="truncate flex-1">{c.name}</span>
                <span className="text-2xs text-[color:var(--text-muted)] tabular-nums">{c.instance_count}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
