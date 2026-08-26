import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, ApiError } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, Button } from "@/components/ui";

// Ontology — the four-tab document (task 6d470cea-e344-40ea-b551-
// c43593ec7db9, epic e39027d3; owner: "not quite there with ontology").
// Replaces the old rail+pills class-browser layout with the prototype's
// document: Structure / Rules / Records / Terms, each reading its own
// GET /api/okf/ontology/<tab> endpoint (a sibling backend slice landing
// separately — a 404 renders an honest "not available yet" line rather
// than a stub). Refresh = POST /rebuild then refetch all four. Tab choice
// persists in localStorage.

type TabKey = "structure" | "rules" | "records" | "terms";
const TAB_STORAGE_KEY = "prism.ontology.tab";
const TABS: { key: TabKey; label: string }[] = [
  { key: "structure", label: "Structure" },
  { key: "rules", label: "Rules" },
  { key: "records", label: "Records" },
  { key: "terms", label: "Terms" },
];

function readStoredTab(): TabKey {
  try {
    const v = localStorage.getItem(TAB_STORAGE_KEY);
    if (v === "structure" || v === "rules" || v === "records" || v === "terms") return v;
  } catch {
    // private mode / storage disabled — fall through to the default
  }
  return "structure";
}

// -- API contract types (sibling slice; /structure, /records, /terms may
// 404 until it lands) ------------------------------------------------------

type StructureClass = {
  id: string; name: string; parent: string | null; comment: string;
  depth: number; count: number; own_count: number; abstract: boolean;
};
type StructureRelation = {
  property: string; label: string; comment: string; domain: string; range: string;
  count: number; example: { from_label: string; to_label: string } | null;
};
type StructurePayload = {
  classes: StructureClass[]; relations: StructureRelation[];
  built_from: { signals: number; tasks: number };
};

type RuleFocus = { iri?: string; label?: string } | string;
type Rule = {
  name: string; title?: string; description: string; looked_at: number;
  violations: number; focus: RuleFocus[];
};
type RulesPayload = { rules: Rule[]; need_decision: number; total: number };

type RecordsClass = { id: string; name: string; count: number; sample: string[] };
type RecordsPayload = { things: number; connections: number; values: number; classes: RecordsClass[] };

type Term = { value: string; in_use: boolean; count: number };
type Vocabulary = { name: string; comment: string; terms: Term[] };
type HeldBack = { vocabulary: string; value: string; count: number };
type TermsPayload = { vocabularies: Vocabulary[]; held_back: HeldBack[] };

type LegacyPayload = { classes: { id: string; name: string }[] };

type SparqlResult = { columns: string[]; bindings: Record<string, unknown>[] };
const DEFAULT_QUERY =
  "PREFIX o: <urn:prism:onto:>\n" +
  "SELECT ?task ?channel WHERE { GRAPH ?g { ?task a o:Task ; o:arrivedVia ?channel } } LIMIT 25";

function notAvailableMessage(e: unknown): string {
  if (e instanceof ApiError && e.status === 404) return "Not available yet.";
  return e instanceof ApiError ? e.message : String(e);
}

function focusLabel(f: RuleFocus): string {
  if (typeof f === "string") return f.split("/").pop() ?? f;
  return f.label ?? f.iri ?? "";
}

// A SectionLabel that also carries a colored dot + a right-aligned count —
// the prototype's "● THINGS   1055 total" row header.
function DotLabel({
  color, trailing, children,
}: { color: string; trailing?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-3">
      <span className="inline-flex items-center gap-1.5 text-2xs uppercase tracking-wider text-[color:var(--text-label)]">
        <i className="inline-block w-1.5 h-1.5 rounded-full shrink-0" style={{ background: color }} />
        {children}
      </span>
      {trailing !== undefined && <span className="text-2xs text-[color:var(--text-muted)]">{trailing}</span>}
    </div>
  );
}

export default function OntologyPage() {
  const [project] = useProject();
  const [tab, setTab] = useState<TabKey>(readStoredTab);

  const [legacy, setLegacy] = useState<LegacyPayload | null>(null);
  const [structure, setStructure] = useState<StructurePayload | null>(null);
  const [structureErr, setStructureErr] = useState<string | null>(null);
  const [rules, setRules] = useState<RulesPayload | null>(null);
  const [rulesErr, setRulesErr] = useState<string | null>(null);
  const [records, setRecords] = useState<RecordsPayload | null>(null);
  const [recordsErr, setRecordsErr] = useState<string | null>(null);
  const [terms, setTerms] = useState<TermsPayload | null>(null);
  const [termsErr, setTermsErr] = useState<string | null>(null);
  const [rebuilding, setRebuilding] = useState(false);

  const [expandedClass, setExpandedClass] = useState<string | null>(null);
  const [instances, setInstances] = useState<Record<string, { id: string; label: string }[]>>({});

  const [query, setQuery] = useState(DEFAULT_QUERY);
  const [result, setResult] = useState<SparqlResult | null>(null);
  const [sparqlError, setSparqlError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const fetchAll = useCallback(() => {
    const qp = `project=${encodeURIComponent(project)}`;

    api.get<LegacyPayload>(`/api/okf/ontology?${qp}`)
      .then(setLegacy).catch(() => setLegacy(null));

    api.get<StructurePayload>(`/api/okf/ontology/structure?${qp}`)
      .then((d) => { setStructure(d); setStructureErr(null); })
      .catch((e) => { setStructure(null); setStructureErr(notAvailableMessage(e)); });

    api.get<{ rules: Rule[]; need_decision?: number; total?: number }>(`/api/okf/ontology/rules?${qp}`)
      .then((raw) => {
        const list = raw.rules ?? [];
        const total = raw.total ?? list.length;
        const need = raw.need_decision ?? list.filter((r) => r.violations > 0).length;
        setRules({ rules: list, total, need_decision: need });
        setRulesErr(null);
      })
      .catch((e) => { setRules(null); setRulesErr(notAvailableMessage(e)); });

    api.get<RecordsPayload>(`/api/okf/ontology/records?${qp}`)
      .then((d) => { setRecords(d); setRecordsErr(null); })
      .catch((e) => { setRecords(null); setRecordsErr(notAvailableMessage(e)); });

    api.get<TermsPayload>(`/api/okf/ontology/terms?${qp}`)
      .then((d) => { setTerms(d); setTermsErr(null); })
      .catch((e) => { setTerms(null); setTermsErr(notAvailableMessage(e)); });
  }, [project]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  useEffect(() => {
    try { localStorage.setItem(TAB_STORAGE_KEY, tab); } catch { /* private mode / storage disabled */ }
  }, [tab]);

  const rebuild = () => {
    setRebuilding(true);
    api.post(`/api/okf/ontology/rebuild?project=${encodeURIComponent(project)}`, {})
      .catch(() => { /* rebuild failed — fetchAll below still shows current state */ })
      .finally(() => { fetchAll(); setRebuilding(false); });
  };

  const toggleInstances = (classId: string) => {
    if (expandedClass === classId) { setExpandedClass(null); return; }
    setExpandedClass(classId);
    if (!instances[classId]) {
      const q = `project=${encodeURIComponent(project)}&class_id=${encodeURIComponent(classId)}&limit=50`;
      api.get<{ instances: { id: string; label: string }[] }>(`/api/okf/ontology/instances?${q}`)
        .then((r) => setInstances((prev) => ({ ...prev, [classId]: r.instances })))
        .catch(() => setInstances((prev) => ({ ...prev, [classId]: [] })));
    }
  };

  const runSparql = () => {
    setRunning(true);
    setSparqlError(null);
    api.post<SparqlResult>(`/api/okf/ontology/sparql?project=${encodeURIComponent(project)}`, { query })
      .then((r) => setResult(r))
      .catch((e) => {
        setResult(null);
        setSparqlError(e instanceof ApiError ? e.message : String(e));
      })
      .finally(() => setRunning(false));
  };

  const counts: Record<TabKey, number> = {
    structure: structure?.classes.length ?? legacy?.classes.length ?? 0,
    rules: rules?.total ?? 0,
    records: records?.things ?? 0,
    terms: terms?.vocabularies.reduce((n, v) => n + v.terms.length, 0) ?? 0,
  };

  return (
    <Page>
      {/* One header row under the app PageHeader (task c37bc70e): the old
          in-page "Ontology" title/subtitle duplicated PageHeader.tsx's own
          title, so it's gone -- tabs sit left, the need-a-decision pill and
          Refresh sit right, exactly like the prototype's header. */}
      <div className="flex items-end justify-between gap-4 flex-wrap border-b border-[color:var(--border-default)]">
        <div className="flex items-center gap-1">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={
                tab === t.key
                  ? "flex items-center gap-2 px-3 py-2 text-[13px] font-medium border-b-2 -mb-px border-[color:var(--text-primary)] text-[color:var(--text-primary)]"
                  : "flex items-center gap-2 px-3 py-2 text-[13px] font-medium border-b-2 -mb-px border-transparent text-[color:var(--text-secondary)] hover:text-[color:var(--text-primary)]"
              }
            >
              {t.label}
              <span className="text-2xs font-mono tabular-nums text-[color:var(--text-muted)]">{counts[t.key]}</span>
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3 pb-2">
          {rules && (
            <span className="inline-flex items-center gap-1.5 text-2xs uppercase tracking-wider text-[color:var(--text-secondary)]">
              <i className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: "var(--social)" }} />
              {rules.need_decision} rules need a decision
            </span>
          )}
          <Button onClick={rebuild} disabled={rebuilding}>{rebuilding ? "Refreshing…" : "Refresh"}</Button>
        </div>
      </div>

      {tab === "structure" && <StructureTab data={structure} error={structureErr} />}
      {tab === "rules" && <RulesTab data={rules} error={rulesErr} />}
      {tab === "records" && (
        <RecordsTab
          data={records} error={recordsErr}
          expandedClass={expandedClass} instances={instances} onToggle={toggleInstances}
          query={query} onQueryChange={setQuery} onRun={runSparql}
          running={running} sparqlError={sparqlError} result={result}
        />
      )}
      {tab === "terms" && <TermsTab data={terms} error={termsErr} />}
    </Page>
  );
}

function StructureTab({ data, error }: { data: StructurePayload | null; error: string | null }) {
  if (error) return <Card><Empty>{error}</Empty></Card>;
  if (!data) return <Card><Empty>Loading structure…</Empty></Card>;
  return (
    <div>
      <p className="text-[13px] text-[color:var(--text-secondary)] max-w-[760px]">
        What this workspace is made of, and how the pieces connect. Built from
        your {data.built_from.signals} queue items and {data.built_from.tasks} tasks,
        and rebuilt every time it syncs.
      </p>
      <div className="grid grid-cols-2 gap-4 mt-4">
        <Card raised>
          <SectionLabel>What is in here</SectionLabel>
          {data.classes.length === 0 ? <Empty>No classes yet.</Empty> : (
            <div className="space-y-1.5">
              {data.classes.map((c) => (
                <div key={c.id} className="flex items-center gap-2" style={{ paddingLeft: c.depth * 16 }}>
                  <span className="ont-node" data-kind="class" data-abstract={c.abstract ? "true" : undefined}>
                    <i className="ont-glyph" />{c.name}
                    <span className="text-2xs font-mono tabular-nums opacity-70">{c.count}</span>
                  </span>
                  <span className="text-[12px] text-[color:var(--text-muted)] truncate">{c.comment}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
        <StructureRelations relations={data.relations} />
      </div>
    </div>
  );
}

function StructureRelations({ relations }: { relations: StructureRelation[] }) {
  const sorted = [...relations].sort((a, b) => b.count - a.count);
  return (
    <Card raised>
      <SectionLabel>How it connects</SectionLabel>
      {sorted.length === 0 ? <Empty>No relations yet.</Empty> : (
        <div className="space-y-4">
          {sorted.map((r) => (
            <div key={r.property} className="ont-edge-card">
              <div className="grid grid-cols-[1fr_auto_1fr] items-baseline gap-3">
                <span className="text-[13px] font-semibold text-[color:var(--text-primary)]">{r.domain}</span>
                <span className="ont-edge-property">{r.property} →</span>
                <span className="text-[13px] font-semibold text-[color:var(--text-primary)] text-right">{r.range}</span>
              </div>
              <div className="flex items-center gap-2 mt-2">
                <span className="ont-edge-count">{r.count}</span>
                <span className="text-[12px] text-[color:var(--text-secondary)]">{r.comment}</span>
              </div>
              {r.example && (
                <div className="text-2xs text-[color:var(--text-muted)] mt-1.5 font-mono">
                  {r.example.from_label} → {r.example.to_label}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function RulesTab({ data, error }: { data: RulesPayload | null; error: string | null }) {
  if (error) return <Card><Empty>{error}</Empty></Card>;
  if (!data) return <Card><Empty>Loading rules…</Empty></Card>;
  const needsDecision = data.rules.filter((r) => r.violations > 0);
  const quiet = data.rules.filter((r) => r.violations === 0);
  return (
    <div className="space-y-4">
      <p className="text-[13px] text-[color:var(--text-secondary)] max-w-[760px]">
        The rules currently in force, each checked against your own data.{" "}
        {data.need_decision} of {data.total} are reporting, and each one is a
        decision waiting on you.
      </p>
      <Card raised>
        <DotLabel color="var(--alarm)" trailing={`${needsDecision.length} of ${data.total}`}>
          Need a decision
        </DotLabel>
        {needsDecision.length === 0 ? <Empty>Nothing needs a decision.</Empty> : (
          <div className="space-y-3">
            {needsDecision.map((r) => <RuleRow key={r.name} rule={r} flagged />)}
          </div>
        )}
      </Card>
      <Card>
        <SectionLabel>Quiet</SectionLabel>
        {quiet.length === 0 ? <Empty>No quiet rules.</Empty> : (
          <div className="space-y-3">
            {quiet.map((r) => <RuleRow key={r.name} rule={r} flagged={false} />)}
          </div>
        )}
      </Card>
    </div>
  );
}

function RuleRow({ rule, flagged }: { rule: Rule; flagged: boolean }) {
  const shown = rule.focus.slice(0, 8);
  const overflow = rule.focus.length - shown.length;
  return (
    <div className="flex items-start gap-3 border-b border-[color:var(--border-default)]/20 pb-3">
      <i
        className="inline-block w-1.5 h-1.5 rounded-full mt-1.5 shrink-0"
        style={{ background: flagged ? "var(--alarm)" : "var(--text-muted)" }}
      />
      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-semibold text-[color:var(--text-primary)]">{rule.title ?? rule.name}</div>
        <div className="text-2xs font-mono text-[color:var(--text-muted)]">{rule.name}</div>
        <div className="text-[12px] text-[color:var(--text-secondary)] mt-1">{rule.description}</div>
        {shown.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2">
            {shown.map((f, i) => (
              <span
                key={i}
                className="text-2xs px-2 py-0.5 rounded-full border"
                style={{ borderColor: "var(--alarm)", color: "var(--alarm)" }}
              >
                {focusLabel(f)}
              </span>
            ))}
            {overflow > 0 && <span className="ont-rolled">+{overflow} more</span>}
          </div>
        )}
      </div>
      <div className="text-2xs text-[color:var(--text-muted)] whitespace-nowrap shrink-0">
        {rule.looked_at} CHECKED · {rule.violations} FAILED
      </div>
    </div>
  );
}

function RecordsTab({
  data, error, expandedClass, instances, onToggle,
  query, onQueryChange, onRun, running, sparqlError, result,
}: {
  data: RecordsPayload | null; error: string | null;
  expandedClass: string | null; instances: Record<string, { id: string; label: string }[]>;
  onToggle: (id: string) => void;
  query: string; onQueryChange: (v: string) => void; onRun: () => void;
  running: boolean; sparqlError: string | null; result: SparqlResult | null;
}) {
  const sparqlBox = (
    <SparqlBox
      query={query} onQueryChange={onQueryChange} onRun={onRun}
      running={running} error={sparqlError} result={result}
    />
  );
  if (error) return <div className="space-y-4"><Card><Empty>{error}</Empty></Card>{sparqlBox}</div>;
  if (!data) return <div className="space-y-4"><Card><Empty>Loading records…</Empty></Card>{sparqlBox}</div>;
  return (
    <div className="space-y-4">
      <p className="text-[13px] text-[color:var(--text-secondary)] max-w-[760px]">
        {data.things} things and {data.connections} connections between them, holding {data.values} values.
      </p>
      <Card raised>
        <DotLabel color="var(--social)" trailing={`${data.things} total`}>Things</DotLabel>
        {data.classes.length === 0 ? <Empty>No records yet.</Empty> : (
          <div className="space-y-2">
            {data.classes.map((c) => (
              <div key={c.id} className="border-b border-[color:var(--border-default)]/20 pb-2">
                <div className="flex items-center gap-3">
                  <span className="text-2xs font-mono tabular-nums w-10 text-right text-[color:var(--text-muted)]">{c.count}</span>
                  <span className="text-[13px] font-semibold text-[color:var(--text-primary)] shrink-0">{c.name}</span>
                  <div className="flex flex-wrap gap-1.5 flex-1 min-w-0">
                    {c.sample.slice(0, 6).map((s, i) => (
                      <span key={i} className="text-2xs px-2 py-0.5 rounded bg-[color:var(--surface-2)] text-[color:var(--text-secondary)] truncate max-w-[220px]">{s}</span>
                    ))}
                  </div>
                  <button
                    type="button"
                    onClick={() => onToggle(c.id)}
                    className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)] shrink-0"
                  >
                    Instances
                  </button>
                </div>
                {expandedClass === c.id && (
                  <div className="flex flex-wrap gap-1.5 mt-2 pl-[52px]">
                    {(instances[c.id] ?? []).length === 0 ? (
                      <span className="text-2xs text-[color:var(--text-muted)]">Loading…</span>
                    ) : instances[c.id].map((i) => (
                      <span key={i.id} className="ont-node" data-kind="instance"><i className="ont-glyph" />{i.label}</span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>
      {sparqlBox}
    </div>
  );
}

function SparqlBox({
  query, onQueryChange, onRun, running, error, result,
}: {
  query: string; onQueryChange: (v: string) => void; onRun: () => void;
  running: boolean; error: string | null; result: SparqlResult | null;
}) {
  return (
    <Card>
      <SectionLabel>Query</SectionLabel>
      <textarea
        value={query}
        onChange={(e) => onQueryChange(e.target.value)}
        rows={5}
        className="w-full text-[13px] font-mono rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)] p-3 leading-relaxed resize-y"
      />
      <div className="mt-2">
        <button
          type="button"
          onClick={onRun}
          disabled={running}
          className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
          style={{ background: "var(--accent-teal-bg)", color: "var(--accent-teal-fg)" }}
        >
          Run
        </button>
      </div>
      {error && <div className="text-[12px] mt-2" style={{ color: "var(--alarm)" }}>{error}</div>}
      {result && (
        <div className="overflow-x-auto mt-3">
          <table className="w-full text-[13px] border-collapse">
            <thead>
              <tr>
                {result.columns.map((c) => (
                  <th
                    key={c}
                    className="text-left text-[11px] uppercase tracking-wide font-semibold px-3 py-2
                      text-[color:var(--text-label)] border-b border-[color:var(--border-default)]"
                  >
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-[color:var(--border-default)]">
              {result.bindings.map((row, i) => (
                <tr key={i}>
                  {result.columns.map((c) => (
                    <td key={c} className="px-3 py-2 align-middle">{String(row[c] ?? "")}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function TermsTab({ data, error }: { data: TermsPayload | null; error: string | null }) {
  if (error) return <Card><Empty>{error}</Empty></Card>;
  if (!data) return <Card><Empty>Loading terms…</Empty></Card>;
  const totalTerms = data.vocabularies.reduce((n, v) => n + v.terms.length, 0);
  return (
    <div className="space-y-4">
      <p className="text-[13px] text-[color:var(--text-secondary)] max-w-[760px]">
        The words this workspace uses, and which of them are in play right
        now. Anything outside these lists is held back rather than filed
        wrongly.
      </p>
      <Card raised>
        <DotLabel color="var(--social)" trailing={`${totalTerms} terms`}>In use</DotLabel>
        {data.vocabularies.length === 0 ? <Empty>No vocabularies yet.</Empty> : (
          <div className="space-y-3">
            {data.vocabularies.map((v) => (
              <div key={v.name} className="flex items-start gap-3 border-b border-[color:var(--border-default)]/20 pb-3">
                <div className="w-32 shrink-0">
                  <div className="text-2xs font-mono text-[color:var(--text-primary)]">{v.name}</div>
                  <div className="text-[12px] text-[color:var(--text-muted)]">{v.comment}</div>
                </div>
                <div className="flex flex-wrap gap-1.5 flex-1 min-w-0">
                  {v.terms.map((t) => (
                    <span
                      key={t.value}
                      className="text-2xs px-2.5 py-0.5 rounded-full border"
                      style={t.in_use
                        ? { borderColor: "var(--social)", color: "var(--social)" }
                        : { borderColor: "var(--border-default)", color: "var(--text-muted)" }}
                    >
                      {t.value}
                    </span>
                  ))}
                </div>
                <span className="text-2xs text-[color:var(--text-muted)] shrink-0">{v.terms.length} terms</span>
              </div>
            ))}
          </div>
        )}
      </Card>
      <Card>
        <SectionLabel>Held back</SectionLabel>
        {data.held_back.length === 0 ? <Empty>Nothing held back.</Empty> : (
          <div className="space-y-1">
            {data.held_back.map((h, i) => (
              <div key={i} className="flex items-center gap-2 text-[13px]">
                <span className="text-[color:var(--text-muted)] font-mono text-2xs">{h.vocabulary}</span>
                <span className="text-[color:var(--text-primary)]">{h.value}</span>
                <span className="ml-auto text-2xs font-mono tabular-nums text-[color:var(--text-muted)]">{h.count}</span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
