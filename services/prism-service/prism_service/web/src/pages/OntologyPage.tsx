import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty } from "@/components/ui";
import OntologyPanel from "@/components/ontology/OntologyPanel";

// Ontology — its own Knowledge surface (task eca23a10, superseding 15c06516's
// toggle inside UnderstandPage; owner: "the explore and the understand and
// the ontology under knowledge is not super clear"). Hosts the existing
// OntologyPanel (classes/instances/properties), a Rules section (every
// axiom, quiet vs violated, with looked_at/detail when present), and a
// SPARQL query box over POST /api/okf/ontology/sparql — that endpoint is
// being built in a sibling slice, so a 404/400 is shown honestly rather
// than stubbed.

type OntAxiom = {
  id: string; name: string; description: string; state: string;
  detail?: string; looked_at?: string;
};
type OntologyPayload = { axioms: OntAxiom[] };

const DEFAULT_QUERY =
  "PREFIX o: <urn:prism:onto:>\n" +
  "SELECT ?task ?channel WHERE { GRAPH ?g { ?task a o:Task ; o:arrivedVia ?channel } } LIMIT 25";

type SparqlResult = { columns: string[]; bindings: Record<string, unknown>[] };

export default function OntologyPage() {
  const [project] = useProject();
  const [axioms, setAxioms] = useState<OntAxiom[]>([]);
  const [query, setQuery] = useState(DEFAULT_QUERY);
  const [result, setResult] = useState<SparqlResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    api.get<OntologyPayload>(`/api/okf/ontology?project=${encodeURIComponent(project)}`)
      .then((d) => setAxioms(d.axioms ?? []))
      .catch(() => setAxioms([]));
  }, [project]);

  const run = () => {
    setRunning(true);
    setError(null);
    api.post<SparqlResult>(`/api/okf/ontology/sparql?project=${encodeURIComponent(project)}`, { query })
      .then((r) => setResult(r))
      .catch((e) => {
        setResult(null);
        // Honest error, not a stub — the sparql endpoint is a sibling slice
        // and may not be merged yet (404) or may reject a query (400).
        setError(e instanceof ApiError ? e.message : String(e));
      })
      .finally(() => setRunning(false));
  };

  return (
    <Page>
      <div>
        <div className="text-lg font-semibold text-[color:var(--text-primary)]">{"Ontology"}</div>
        <div className="text-[13px] text-[color:var(--text-secondary)] mt-1 max-w-[760px]">
          Classes, rules, and queries over the projected ontology.
        </div>
      </div>

      <Card raised>
        <OntologyPanel project={project} />
      </Card>

      <Card>
        <SectionLabel>Rules</SectionLabel>
        {axioms.length === 0 ? (
          <Empty>No axioms projected yet.</Empty>
        ) : (
          <div className="space-y-2">
            {axioms.map((a) => (
              <div key={a.id} className="text-[13px] border-b border-[color:var(--border-default)]/20 py-1.5">
                <div className="flex items-center gap-2">
                  <span
                    className="ont-axiom"
                    data-state={a.state}
                    style={a.state === "violated" ? { color: "var(--alarm)" } : undefined}
                  >
                    {a.name}
                  </span>
                  <span className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)]">{a.state}</span>
                </div>
                {a.looked_at && (
                  <div className="text-2xs text-[color:var(--text-muted)] mt-0.5">Looked at: {a.looked_at}</div>
                )}
                {a.detail && (
                  <div className="text-2xs text-[color:var(--text-muted)] mt-0.5">{a.detail}</div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <SectionLabel>Query</SectionLabel>
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          rows={5}
          className="w-full text-[13px] font-mono rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)] p-3 leading-relaxed resize-y"
        />
        <div className="mt-2">
          <button
            type="button"
            onClick={run}
            disabled={running}
            className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
            style={{ background: "var(--accent-teal-bg)", color: "var(--accent-teal-fg)" }}
          >
            Run
          </button>
        </div>
        {error && (
          <div className="text-[12px] mt-2" style={{ color: "var(--alarm)" }}>
            {error}
          </div>
        )}
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
    </Page>
  );
}
