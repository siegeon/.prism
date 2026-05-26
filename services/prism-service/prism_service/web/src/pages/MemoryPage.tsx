import { useEffect, useState, useMemo } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, Kpi, SectionLabel, Pill, Empty } from "@/components/ui";

type Entry = {
  id?: string;
  name?: string;
  type?: string;
  classification?: string;
  status?: string;
  description?: string;
  domain?: string;
  importance?: number;
  memory_type?: string;
  recall_count?: number;
  last_recalled?: string;
  effectiveness?: number;
  valid_at?: string;
  invalid_at?: string;
  generation?: number;
  evidence?: Record<string, unknown>;
};

const TYPES = ["all", "expertise", "convention", "decision", "anti-pattern"];
const STATUSES = ["all", "active", "stale", "retired"];

// v6.0.21 — semantic chip palette. Each memory-type/status/classification
// label maps to one of the --accent-* token triples (bg / ring / fg) in
// index.css so the page reads as more than "blue on blue on blue".
// Unknown labels fall back to slate (the old --midground-base look).
type ChipTone =
  | "teal" | "sage" | "amber" | "rose" | "violet" | "emerald" | "slate";

const TYPE_TONE: Record<string, ChipTone> = {
  expertise: "teal",
  convention: "sage",
  decision: "amber",
  "anti-pattern": "rose",
  feedback: "violet",
  project: "amber",
  reference: "teal",
  user: "violet",
};

const STATUS_TONE: Record<string, ChipTone> = {
  active: "emerald",
  stale: "amber",
  retired: "slate",
};

const TONE_CLASS: Record<ChipTone, string> = {
  teal:    "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)] ring-1 ring-inset ring-[color:var(--accent-teal-ring)]",
  sage:    "bg-[color:var(--accent-sage-bg)] text-[color:var(--accent-sage-fg)] ring-1 ring-inset ring-[color:var(--accent-sage-ring)]",
  amber:   "bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)] ring-1 ring-inset ring-[color:var(--accent-amber-ring)]",
  rose:    "bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] ring-1 ring-inset ring-[color:var(--accent-rose-ring)]",
  violet:  "bg-[color:var(--accent-violet-bg)] text-[color:var(--accent-violet-fg)] ring-1 ring-inset ring-[color:var(--accent-violet-ring)]",
  emerald: "bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)] ring-1 ring-inset ring-[color:var(--accent-emerald-ring)]",
  slate:   "bg-[color:var(--accent-slate-bg)] text-[color:var(--accent-slate-fg)] ring-1 ring-inset ring-[color:var(--accent-slate-ring)]",
};

function chipClass(tone: ChipTone): string {
  return `text-[10px] uppercase tracking-wider px-2 py-0.5 rounded ${TONE_CLASS[tone]}`;
}

// Importance 1-10 → a single colored dot. Low importance reads as
// muted slate, mid as sage, high as amber, top as rose. Adds a visual
// signal next to the "imp N" text without growing the row height.
function importanceTone(n: number): ChipTone {
  if (n >= 9) return "rose";
  if (n >= 7) return "amber";
  if (n >= 4) return "sage";
  return "slate";
}

export default function MemoryPage() {
  const [project] = useProject();
  const [domains, setDomains] = useState<string[]>([]);
  const [stats, setStats] = useState<Record<string, { active: number; archived: number; total: number }>>({});
  const [domain, setDomain] = useState<string>("all");
  const [type, setType] = useState<string>("all");
  const [status, setStatus] = useState<string>("all");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [bump, setBump] = useState(0);
  const [open, setOpen] = useState<Set<string>>(new Set());

  const toggleOpen = (id: string) => {
    const next = new Set(open);
    if (next.has(id)) next.delete(id); else next.add(id);
    setOpen(next);
  };

  useEffect(() => {
    api.get<{ domains: string[]; stats: Record<string, { active: number; archived: number; total: number }> }>(`/api/memory/domains?project=${project}`)
      .then((d) => { setDomains(d.domains); setStats(d.stats); })
      .catch(() => { setDomains([]); setStats({}); });
  }, [project, bump]);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(t);
  }, [notice]);

  const importClaudeMemories = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ imported: number; skipped: number; failed: number; memory_dir: string }>(
        `/api/memory/import-claude-memories?project=${project}`, {},
      );
      setNotice(
        r.imported > 0
          ? `Imported ${r.imported} memor${r.imported === 1 ? "y" : "ies"} from ${r.memory_dir} — skipped ${r.skipped}, failed ${r.failed}. Re-running is idempotent (supersedes, not duplicates).`
          : `No new memories imported from ${r.memory_dir} (${r.skipped} skipped, ${r.failed} failed). All already present, or directory empty.`
      );
      setBump((b) => b + 1);
    } catch (e) {
      setNotice(`Import failed: ${(e as Error).message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    const params = new URLSearchParams({ project });
    if (domain !== "all") params.set("domain", domain);
    if (type !== "all") params.set("type", type);
    if (status !== "all") params.set("status", status);
    api.get<{ entries: Entry[] }>(`/api/memory/entries?${params}`)
      .then((d) => setEntries(d.entries)).catch(() => setEntries([]));
  }, [project, domain, type, status, bump]);

  // v6.0.15 — stats values are {active, archived, total} objects since
  // the v6.0.8 import added Graphiti supersede counts. Summing the raw
  // values with `+` coerces them to strings and renders "[object Object]".
  // Sum .active so the Entries Kpi matches what's actually filterable.
  const total = useMemo(
    () => Object.values(stats).reduce((a, b) => a + (b?.active ?? 0), 0),
    [stats],
  );

  return (
    <Page>
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm opacity-60 flex-1">
          Persisted patterns, conventions, decisions, and failures —
          queried by every Claude session via <code className="opacity-80">memory_recall</code>.
          Backfill from Claude Code's auto-memory if this page is empty.
        </p>
        <button
          onClick={importClaudeMemories}
          disabled={busy}
          className="px-4 py-2 rounded-md bg-[color:var(--midground-base)] text-[color:var(--background-base)] text-xs uppercase tracking-wider disabled:opacity-40 shrink-0"
        >
          {busy ? "Working…" : "Import Claude memories"}
        </button>
      </div>

      {notice && (
        <div className="fixed bottom-6 right-6 z-40 max-w-[420px] rounded-md border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)]/95 backdrop-blur-sm shadow-lg px-4 py-3 text-[12px] flex items-start gap-3">
          <span className="flex-1 opacity-90 leading-relaxed">{notice}</span>
          <button
            onClick={() => setNotice(null)}
            className="text-[10px] uppercase tracking-wider opacity-60 hover:opacity-100 shrink-0"
          >
            dismiss
          </button>
        </div>
      )}

      <section className="flex flex-wrap gap-3">
        <Kpi label="Entries" value={total} />
        <Kpi label="Domains" value={domains.length} />
        <Kpi label="Showing" value={entries.length} />
      </section>

      <Card>
        <SectionLabel>Domain</SectionLabel>
        <div className="flex flex-wrap gap-2 mb-4">
          <Pill active={domain === "all"} onClick={() => setDomain("all")}>all</Pill>
          {domains.map((d) => (
            <Pill key={d} active={domain === d} onClick={() => setDomain(d)}>{d}</Pill>
          ))}
        </div>
        <SectionLabel>Type</SectionLabel>
        <div className="flex flex-wrap gap-2 mb-4">
          {TYPES.map((t) => (
            <Pill key={t} active={type === t} onClick={() => setType(t)} tone={TYPE_TONE[t]}>
              {t}
            </Pill>
          ))}
        </div>
        <SectionLabel>Status</SectionLabel>
        <div className="flex flex-wrap gap-2">
          {STATUSES.map((s) => (
            <Pill key={s} active={status === s} onClick={() => setStatus(s)} tone={STATUS_TONE[s]}>
              {s}
            </Pill>
          ))}
        </div>
      </Card>

      <Card>
        <SectionLabel>{entries.length} entr{entries.length === 1 ? "y" : "ies"}</SectionLabel>
        {entries.length === 0 ? (
          <Empty>No entries match these filters.</Empty>
        ) : (
          <div className="divide-y divide-[color:var(--midground-base)]/10">
            {entries.map((e, i) => {
              const key = e.id ?? `${e.name}-${i}`;
              const isOpen = open.has(key);
              return (
                <div key={key} className="py-3">
                  <button
                    onClick={() => toggleOpen(key)}
                    className="w-full text-left flex items-center gap-3 text-sm hover:opacity-100"
                  >
                    <span className="text-xs opacity-50 w-4">{isOpen ? "▾" : "▸"}</span>
                    <span className="font-medium flex-1 truncate">{e.name ?? "—"}</span>
                    {typeof e.importance === "number" && (
                      <span className="text-[10px] uppercase tracking-wider opacity-70 flex items-center gap-1.5" title="importance 1-10">
                        <span
                          className="inline-block w-2 h-2 rounded-full"
                          style={{ background: `var(--accent-${importanceTone(e.importance)}-fg)` }}
                          aria-hidden
                        />
                        imp {e.importance}
                      </span>
                    )}
                    {typeof e.recall_count === "number" && e.recall_count > 0 && (
                      <span className="text-[10px] uppercase tracking-wider opacity-50" title="times recalled">
                        ↻ {e.recall_count}
                      </span>
                    )}
                    {e.type && (
                      <span className={chipClass(TYPE_TONE[e.type.toLowerCase()] ?? "slate")}>
                        {e.type}
                      </span>
                    )}
                    {e.classification && (
                      <span className={chipClass(TYPE_TONE[e.classification.toLowerCase()] ?? "slate")}>
                        {e.classification}
                      </span>
                    )}
                    {e.status && (
                      <span className={chipClass(STATUS_TONE[e.status.toLowerCase()] ?? "slate")}>
                        {e.status}
                      </span>
                    )}
                  </button>
                  {!isOpen && e.description && (
                    <div className="text-xs opacity-60 mt-1 ml-7 line-clamp-2">{e.description}</div>
                  )}
                  {isOpen && (
                    <div className="ml-7 mt-2 space-y-3">
                      {e.description && (
                        <pre className="whitespace-pre-wrap text-[12px] leading-relaxed opacity-90 font-sans p-3 rounded-md bg-[color:var(--midground-base)]/5 border border-[color:var(--midground-base)]/10">
                          {e.description}
                        </pre>
                      )}
                      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px]">
                        {e.id && <div><span className="opacity-50">id:</span> <span className="font-mono">{e.id}</span></div>}
                        {e.memory_type && <div><span className="opacity-50">memory_type:</span> {e.memory_type}</div>}
                        {typeof e.effectiveness === "number" && (
                          <div>
                            <span className="opacity-50">effectiveness:</span>{" "}
                            <span className={e.effectiveness > 0 ? "text-emerald-300/90" : e.effectiveness < 0 ? "text-rose-300/90" : "opacity-70"}>
                              {e.effectiveness.toFixed(2)}
                            </span>
                          </div>
                        )}
                        {typeof e.generation === "number" && <div><span className="opacity-50">gen:</span> {e.generation}</div>}
                        {e.valid_at && <div><span className="opacity-50">valid since:</span> {e.valid_at.slice(0, 10)}</div>}
                        {e.last_recalled && <div><span className="opacity-50">last recalled:</span> {e.last_recalled.slice(0, 10)}</div>}
                      </div>
                      {e.evidence && Object.keys(e.evidence).length > 0 && (
                        <div className="text-[11px]">
                          <span className="opacity-50">evidence:</span>
                          <pre className="mt-1 p-2 rounded bg-[color:var(--midground-base)]/5 border border-[color:var(--midground-base)]/10 whitespace-pre-wrap font-mono opacity-80">
                            {JSON.stringify(e.evidence, null, 2)}
                          </pre>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </Page>
  );
}
