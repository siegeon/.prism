import { useEffect, useState, useMemo, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import {
  Page, Card, Kpi, SectionLabel, Pill, Empty, toneFromLabel,
  type PillTone,
} from "@/components/ui";

type Entry = {
  id?: string;
  name?: string;
  type?: string;
  classification?: string;
  status?: string;
  description?: string;
  summary?: string;
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
const TYPE_TONE: Record<string, PillTone> = {
  expertise: "teal",
  convention: "sage",
  decision: "amber",
  "anti-pattern": "rose",
  feedback: "violet",
  project: "amber",
  reference: "teal",
  user: "violet",
  pattern: "teal",
  failure: "rose",
};

const STATUS_TONE: Record<string, PillTone> = {
  active: "emerald",
  stale: "amber",
  retired: "slate",
  archived: "slate",
  needs_review: "amber",
};

// Importance 1-10 → a single colored dot. Low importance reads as
// muted slate, mid as sage, high as amber, top as rose.
function importanceTone(n: number): PillTone {
  if (n >= 9) return "rose";
  if (n >= 7) return "amber";
  if (n >= 4) return "sage";
  return "slate";
}

export default function MemoryPage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const [domains, setDomains] = useState<string[]>([]);
  const [stats, setStats] = useState<Record<string, { active: number; archived: number; total: number }>>({});
  const [domain, setDomain] = useState<string>("all");
  const [type, setType] = useState<string>("all");
  const [status, setStatus] = useState<string>("all");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [bump, setBump] = useState(0);

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
    const load = () => api.get<{ entries: Entry[] }>(`/api/memory/entries?${params}`)
      .then((d) => setEntries(d.entries)).catch(() => { /* keep last */ });
    load();
    // Re-fetch every 30s so the summary-worker's results appear without
    // a manual refresh while the user is looking at the page.
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [project, domain, type, status, bump]);

  // v6.0.15 — stats values are {active, archived, total} objects since
  // the v6.0.8 import added Graphiti supersede counts. Sum .active.
  const total = useMemo(
    () => Object.values(stats).reduce((a, b) => a + (b?.active ?? 0), 0),
    [stats],
  );
  const pendingSummaries = useMemo(
    () => entries.filter((e) => !e.summary).length,
    [entries],
  );

  return (
    <Page>
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm opacity-60 flex-1">
          Persisted patterns, conventions, decisions, and failures —
          queried by every Claude session via <code className="opacity-80">memory_recall</code>.
          The summary on each tile is minted by the memory-summary worker;
          click a tile for the full description, evidence, and supersede chain.
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
        <Kpi label="Awaiting summary" value={pendingSummaries} />
      </section>

      <Card>
        <SectionLabel>Domain</SectionLabel>
        <div className="flex flex-wrap gap-2 mb-4">
          <Pill active={domain === "all"} onClick={() => setDomain("all")} tone="slate">all</Pill>
          {domains.map((d) => (
            <Pill key={d} active={domain === d} onClick={() => setDomain(d)} tone={toneFromLabel(d)}>
              {d}
            </Pill>
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
          <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-3">
            {entries.map((e, i) => (
              <MemoryTile
                key={e.id ?? `${e.name}-${i}`}
                entry={e}
                onClick={() => e.id && navigate(`/memory/${e.id}`)}
              />
            ))}
          </div>
        )}
      </Card>
    </Page>
  );
}

// ---------------------------------------------------------------------------
// MemoryTile — uniform-sized card (v6.1.1)
//
// Mirrors the conductor TaskTile pattern (auto-fill grid w/ minmax 280px)
// so /memory and /conductor read as visual siblings. Each tile shows:
//   - name (1-line clamp, bold)
//   - summary line — the worker-minted plain-English rephrase, or a
//     "Summarizing…" placeholder when the worker hasn't filled it yet
//   - type / status / classification chips (semantic palette)
//   - importance dot + recall ↻ count + domain in the meta footer
// Whole tile is a button that routes to /memory/:id for the drill-in.
// ---------------------------------------------------------------------------
function MemoryTile({ entry, onClick }: { entry: Entry; onClick: () => void }) {
  const type = (entry.type ?? "").toLowerCase();
  const status = (entry.status ?? "").toLowerCase();
  const classification = (entry.classification ?? "").toLowerCase();
  const typeTone: PillTone = TYPE_TONE[type] ?? "slate";
  const statusTone: PillTone = STATUS_TONE[status] ?? "slate";
  const importance = entry.importance ?? 0;
  const recall = entry.recall_count ?? 0;
  const tooltip = `${entry.name ?? "—"}\nid: ${entry.id ?? "—"}\n\n${entry.description ?? ""}`;
  return (
    <button
      onClick={onClick}
      title={tooltip}
      className="text-left rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:border-[color:var(--border-strong)] p-3 flex flex-col gap-2 transition-colors min-h-[140px]"
    >
      <div className="text-[13px] leading-snug font-medium line-clamp-1 text-[color:var(--text-primary)]">
        {entry.name ?? "—"}
      </div>
      <div className={
        "text-[12px] leading-relaxed line-clamp-3 flex-1 " +
        (entry.summary
          ? "text-[color:var(--text-secondary)]"
          : "text-[color:var(--text-muted)] italic")
      }>
        {entry.summary || "Summarizing…"}
      </div>
      <div className="flex flex-wrap items-center gap-1">
        {type && <TileBadge tone={typeTone}>{type}</TileBadge>}
        {status && status !== "active" && (
          <TileBadge tone={statusTone}>{status}</TileBadge>
        )}
        {classification && (
          <TileBadge tone="slate">{classification}</TileBadge>
        )}
      </div>
      <div className="flex items-center gap-2 text-[11px] font-mono text-[color:var(--text-muted)]">
        {importance > 0 && (
          <span className="inline-flex items-center gap-1" title={`importance ${importance}/10`}>
            <span
              className="inline-block w-1.5 h-1.5 rounded-full"
              style={{ background: `var(--accent-${importanceTone(importance)}-fg)` }}
              aria-hidden
            />
            imp {importance}
          </span>
        )}
        {recall > 0 && (
          <span title={`recalled ${recall} time${recall === 1 ? "" : "s"}`}>↻ {recall}</span>
        )}
        {entry.domain && (
          <span className="ml-auto truncate text-[color:var(--text-secondary)]" title={`domain: ${entry.domain}`}>
            {entry.domain}
          </span>
        )}
      </div>
    </button>
  );
}

function TileBadge({ tone, children }: { tone: PillTone; children: ReactNode }) {
  return (
    <span
      className="text-[10px] uppercase tracking-wider font-mono px-1.5 py-0.5 rounded ring-1"
      style={{
        background: `var(--accent-${tone}-bg)`,
        color: `var(--accent-${tone}-fg)`,
        boxShadow: `inset 0 0 0 1px var(--accent-${tone}-ring)`,
      }}
    >
      {children}
    </span>
  );
}
