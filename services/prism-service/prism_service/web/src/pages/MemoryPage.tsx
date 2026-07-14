import { useEffect, useState, useMemo, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import {
  Page, Card, Kpi, SectionLabel, Pill, Empty, toneFromLabel,
  type PillTone,
} from "@/components/ui";
import { domainTone, importanceTone } from "@/lib/domainTone";
import { relativeTime } from "@/lib/relativeTime";

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
  // Server-computed activation score (Tier 1): importance + effectiveness*W1
  // - Ebbinghaus decay(now - last_recalled)*W2. Higher = hotter; entries that
  // fall below the fade threshold drift into the Fading lane.
  activation?: number;
  valid_at?: string;
  invalid_at?: string;
  generation?: number;
  evidence?: Record<string, unknown>;
};

type GroupKey = "value" | "classification" | "domain" | "type";

const GROUPS: { id: GroupKey; label: string }[] = [
  { id: "value", label: "by value" },
  { id: "classification", label: "by classification" },
  { id: "domain", label: "by domain" },
  { id: "type", label: "by type" },
];

const TYPES = ["all", "expertise", "convention", "decision", "anti-pattern"];
// Match the statuses MemoryService actually writes. The old list used
// 'stale'/'retired', which no entry ever has, so clicking them always showed
// "No entries match" — and the page defaulted to 'all', dumping every archived
// generation (1.3MB) alongside the active set.
const STATUSES = ["all", "active", "archived", "needs_review"];

// Composite value score — what "by value" means in concrete terms.
//   - importance (1-10) is the declared value the author assigned at write
//   - recall_count is proven usefulness (someone's session pulled it)
//   - effectiveness (-1..1) is outcome correlation from completed tasks
//   - generation > 1 indicates the memory has been refined, which is
//     itself a signal of sustained relevance
// Weighted so a memory with imp=7, recall=3, eff=0.5 (≈14) beats one
// with imp=10, recall=0, eff=0 (=10): proven beats declared.
function valueScore(e: Entry): number {
  const imp = e.importance ?? 0;
  const recall = e.recall_count ?? 0;
  const eff = e.effectiveness ?? 0;
  const gen = Math.max(0, (e.generation ?? 1) - 1);
  return imp + recall * 1.5 + eff * 5 + gen * 0.5;
}

// v6.2.19 — entries whose server-computed activation has decayed below this
// fade line surface in their own muted lane so memory decay is VISIBLE in the
// SPA (UI-FIRST), not headless. Mirrors the /api/memory/fading default.
const FADE_THRESHOLD = 6.0;

type ValueBucketId = "proven" | "high" | "rest" | "fading";

// v6.1.2 — "by value" view tiers entries into buckets so the most useful
// memories sit at the top instead of getting buried in a 47-tile wall.
// Recalled memories are *proven* — they came up in real sessions — so they
// always lead, regardless of declared importance. v6.2.19 adds a Fading lane:
// an entry whose activation has decayed below the fade line (and which isn't
// proven) drifts here so its decay is legible.
function valueBucket(e: Entry): ValueBucketId {
  if ((e.recall_count ?? 0) > 0) return "proven";
  if (e.activation !== undefined && e.activation < FADE_THRESHOLD) return "fading";
  if ((e.importance ?? 0) >= 7) return "high";
  return "rest";
}

const VALUE_BUCKETS: { id: ValueBucketId; label: string; blurb: string }[] = [
  { id: "proven", label: "Proven",   blurb: "Recalled in real sessions — these have earned their tile." },
  { id: "high",   label: "High value", blurb: "Author marked importance ≥ 7. Not yet recalled." },
  { id: "rest",   label: "Resting",   blurb: "Lower importance and untouched so far. Quiet, not useless." },
  { id: "fading", label: "Fading",    blurb: "Activation has decayed below the fade line — recall to revive, or let the pruner forget." },
];

export default function MemoryPage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const [domains, setDomains] = useState<string[]>([]);
  const [stats, setStats] = useState<Record<string, { active: number; archived: number; total: number }>>({});
  const [domain, setDomain] = useState<string>("all");
  const [type, setType] = useState<string>("all");
  // Default to the active set: it matches the "Entries" KPI and is a fraction
  // of the payload (the full 'all' set includes every archived generation).
  const [status, setStatus] = useState<string>("active");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [bump, setBump] = useState(0);
  const [groupBy, setGroupBy] = useState<GroupKey>("value");

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

  // Backfill — enqueue every summary-less memory onto the learning pipeline's
  // memory.written bus so the budget-governed consumer pool mints one-liners
  // over time. Idempotent; only summary-less active entries enqueue.
  const generateSummaries = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ queued: number; project: string }>(
        `/api/memory/backfill-summaries?project=${project}`, {},
      );
      setNotice(
        r.queued > 0
          ? `Queued ${r.queued} memor${r.queued === 1 ? "y" : "ies"} for summarization — one-liners mint over time as the budget-governed pipeline drains.`
          : `No summary-less memories to queue — every shown memory already has a summary.`
      );
    } catch (e) {
      setNotice(`Backfill failed: ${(e as Error).message ?? e}`);
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
      .then((d) => { setEntries(d.entries); setLoaded(true); })
      .catch(() => { setLoaded(true); /* keep last entries */ });
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [project, domain, type, status, bump]);

  const total = useMemo(
    () => Object.values(stats).reduce((a, b) => a + (b?.active ?? 0), 0),
    [stats],
  );
  // Summaries are an OPTIONAL upgrade, not the tile face. Surface a truthful
  // coverage count (N summarized of M shown) instead of fake "awaiting" work.
  const summarizedCount = useMemo(
    () => entries.filter((e) => e.summary).length,
    [entries],
  );
  const pendingSummaries = useMemo(
    () => entries.filter((e) => !e.summary).length,
    [entries],
  );
  const provenCount = useMemo(
    () => entries.filter((e) => (e.recall_count ?? 0) > 0).length,
    [entries],
  );

  // Group + sort. Each group's entries are sorted by valueScore desc so
  // even within a domain or classification the most useful float up.
  const grouped = useMemo<{ id: string; label: string; blurb?: string; tone?: PillTone; items: Entry[] }[]>(() => {
    if (entries.length === 0) return [];
    const buckets: Record<string, Entry[]> = {};
    const order: string[] = [];
    const push = (key: string, e: Entry) => {
      if (!(key in buckets)) { buckets[key] = []; order.push(key); }
      buckets[key].push(e);
    };
    if (groupBy === "value") {
      for (const b of VALUE_BUCKETS) buckets[b.id] = [];
      for (const e of entries) push(valueBucket(e), e);
      return VALUE_BUCKETS
        .filter((b) => buckets[b.id]?.length)
        .map((b) => ({
          id: b.id, label: b.label, blurb: b.blurb,
          tone: b.id === "proven" ? "emerald" as PillTone
            : b.id === "high" ? "amber" as PillTone
            : b.id === "fading" ? "slate" as PillTone
            : "slate" as PillTone,
          items: [...buckets[b.id]].sort((a, x) => valueScore(x) - valueScore(a)),
        }));
    }
    const keyOf = (e: Entry): string => {
      if (groupBy === "classification") return (e.classification || "unclassified").toLowerCase();
      if (groupBy === "domain") return (e.domain || "—").toLowerCase();
      return (e.type || "—").toLowerCase();
    };
    for (const e of entries) push(keyOf(e), e);
    // Stable group order: by group size desc, ties broken by name.
    order.sort((a, b) => buckets[b].length - buckets[a].length || a.localeCompare(b));
    return order.map((k) => ({
      id: k,
      label: k,
      tone:
        groupBy === "classification" ? (domainTone("classification", k) ?? "slate")
        : groupBy === "type" ? (domainTone("memoryType", k) ?? "slate")
        : toneFromLabel(k),
      items: [...buckets[k]].sort((a, x) => valueScore(x) - valueScore(a)),
    }));
  }, [entries, groupBy]);

  return (
    <Page>
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm opacity-60 flex-1">
          Persisted patterns, conventions, decisions, and failures —
          queried by every Claude session via <code className="opacity-80">memory_recall</code>.
          Tiles lead with <em>proven</em> memories (recalled in real sessions);
          click any tile for the full description, evidence, and supersede chain.
        </p>
        <div className="flex items-center gap-2 shrink-0">
          {pendingSummaries > 0 && (
            <button
              onClick={generateSummaries}
              disabled={busy}
              className="px-4 py-2 rounded-md border border-[color:var(--midground-base)]/40 text-xs uppercase tracking-wider disabled:opacity-40"
            >
              {busy ? "Working…" : `Generate summaries (${pendingSummaries})`}
            </button>
          )}
          <button
            onClick={importClaudeMemories}
            disabled={busy}
            className="px-4 py-2 rounded-md bg-[color:var(--midground-base)] text-[color:var(--background-base)] text-xs uppercase tracking-wider disabled:opacity-40"
          >
            {busy ? "Working…" : "Import Claude memories"}
          </button>
        </div>
      </div>

      {notice && (
        <div className="fixed bottom-6 right-6 z-40 max-w-[420px] rounded-md border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)]/95 backdrop-blur-sm shadow-lg px-4 py-3 text-[12px] flex items-start gap-3">
          <span className="flex-1 opacity-90 leading-relaxed">{notice}</span>
          <button
            onClick={() => setNotice(null)}
            className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100 shrink-0"
          >
            dismiss
          </button>
        </div>
      )}

      <section className="flex flex-wrap gap-3">
        <Kpi label="Entries" value={total} />
        <Kpi label="Domains" value={domains.length} />
        <Kpi label="Showing" value={entries.length} />
        <Kpi label="Proven (recalled)" value={provenCount} />
        <Kpi label="Summaries" value={`${summarizedCount}/${entries.length}`} />
      </section>

      <Card>
        <SectionLabel>Group</SectionLabel>
        <div className="flex flex-wrap gap-2 mb-4">
          {GROUPS.map((g) => (
            <Pill key={g.id} active={groupBy === g.id} onClick={() => setGroupBy(g.id)} tone="teal">
              {g.label}
            </Pill>
          ))}
        </div>
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
            <Pill key={t} active={type === t} onClick={() => setType(t)} tone={domainTone("memoryType", t)}>
              {t}
            </Pill>
          ))}
        </div>
        <SectionLabel>Status</SectionLabel>
        <div className="flex flex-wrap gap-2">
          {STATUSES.map((s) => (
            <Pill key={s} active={status === s} onClick={() => setStatus(s)} tone={domainTone("memoryStatus", s)}>
              {s}
            </Pill>
          ))}
        </div>
      </Card>

      {!loaded ? (
        <Card><Empty>Loading memories…</Empty></Card>
      ) : entries.length === 0 ? (
        <Card><Empty>No entries match these filters.</Empty></Card>
      ) : (
        grouped.map((g) => (
          <Card key={g.id}>
            <div className="flex items-baseline gap-3 flex-wrap mb-3">
              <span
                className="text-2xs uppercase tracking-wider font-mono px-2 py-0.5 rounded ring-1"
                style={{
                  background: `var(--accent-${g.tone ?? "slate"}-bg)`,
                  color: `var(--accent-${g.tone ?? "slate"}-fg)`,
                  boxShadow: `inset 0 0 0 1px var(--accent-${g.tone ?? "slate"}-ring)`,
                }}
              >
                {g.label}
              </span>
              <span className="text-2xs opacity-60 font-mono">
                {g.items.length} entr{g.items.length === 1 ? "y" : "ies"}
              </span>
              {g.blurb && (
                <span className="text-2xs opacity-50 flex-1 min-w-0">{g.blurb}</span>
              )}
            </div>
            <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-3">
              {g.items.map((e, i) => (
                <MemoryTile
                  key={e.id ?? `${e.name}-${i}`}
                  entry={e}
                  onClick={() => e.id && navigate(`/memory/${e.id}`)}
                />
              ))}
            </div>
          </Card>
        ))
      )}
    </Page>
  );
}

// ---------------------------------------------------------------------------
// MemoryTile — uniform-sized card (v6.1.2)
//
// Surfaces the metadata that's been sitting unused on ExpertiseEntry:
// importance dot, recall counter (bolded when proven), effectiveness
// indicator when non-zero, last-recalled relative time, generation chip
// when > 1, memory_type icon. Plus the existing type / status /
// classification chips. Whole tile routes to /memory/:id.
// ---------------------------------------------------------------------------
function MemoryTile({ entry, onClick }: { entry: Entry; onClick: () => void }) {
  const type = (entry.type ?? "").toLowerCase();
  const status = (entry.status ?? "").toLowerCase();
  const classification = (entry.classification ?? "").toLowerCase();
  const typeTone: PillTone = domainTone("memoryType", type) ?? "slate";
  const statusTone: PillTone = domainTone("memoryStatus", status) ?? "slate";
  const classTone: PillTone = domainTone("classification", classification) ?? "slate";
  const importance = entry.importance ?? 0;
  const recall = entry.recall_count ?? 0;
  const effectiveness = entry.effectiveness ?? 0;
  const generation = entry.generation ?? 1;
  const proven = recall > 0;
  const activation = entry.activation;
  const fading = activation !== undefined && activation < FADE_THRESHOLD && !proven;
  const tooltip = `${entry.name ?? "—"}\nid: ${entry.id ?? "—"}\n\n${entry.description ?? ""}`;
  return (
    <button
      onClick={onClick}
      title={tooltip}
      className={
        "text-left rounded-md border bg-[color:var(--surface-2)] p-3 flex flex-col gap-2 transition-colors min-h-[140px] " +
        (proven
          ? "border-[color:var(--accent-emerald-ring)] hover:border-[color:var(--accent-emerald-fg)]"
          : "border-[color:var(--border-default)] hover:border-[color:var(--border-strong)]")
      }
    >
      <div className="flex items-start gap-2">
        <div className="text-[13px] leading-snug font-medium line-clamp-1 text-[color:var(--text-primary)] flex-1 min-w-0">
          {entry.name ?? "—"}
        </div>
        {generation > 1 && (
          <span
            className="text-2xs font-mono px-1 py-0.5 rounded bg-[color:var(--surface-3)] text-[color:var(--text-muted)] shrink-0"
            title={`generation ${generation} — superseded ${generation - 1} earlier version${generation === 2 ? "" : "s"}`}
          >
            gen {generation}
          </span>
        )}
      </div>
      <div className={
        "text-[12px] leading-relaxed line-clamp-3 flex-1 " +
        (entry.summary
          ? "text-[color:var(--text-secondary)]"
          : "text-[color:var(--text-muted)] italic")
      }>
        {entry.summary || entry.description}
      </div>
      <div className="flex flex-wrap items-center gap-1">
        {type && <TileBadge tone={typeTone}>{type}</TileBadge>}
        {classification && <TileBadge tone={classTone}>{classification}</TileBadge>}
        {status && status !== "active" && (
          <TileBadge tone={statusTone}>{status}</TileBadge>
        )}
      </div>
      <div className="flex items-center gap-2 text-2xs font-mono text-[color:var(--text-muted)]">
        {activation !== undefined && (
          <span
            className={
              "inline-flex items-center gap-1 px-1.5 py-0.5 rounded ring-1 " +
              (fading
                ? "text-[color:var(--accent-slate-fg)]"
                : "text-[color:var(--text-secondary)]")
            }
            style={{
              background: `var(--accent-${fading ? "slate" : "teal"}-bg)`,
              boxShadow: `inset 0 0 0 1px var(--accent-${fading ? "slate" : "teal"}-ring)`,
            }}
            title={`activation ${activation.toFixed(2)}${fading ? " — fading (decayed below the fade line)" : ""}`}
          >
            act {activation.toFixed(1)}
          </span>
        )}
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
        {proven ? (
          <span
            className="text-[color:var(--accent-emerald-fg)] font-semibold"
            title={`recalled ${recall} time${recall === 1 ? "" : "s"}${entry.last_recalled ? ` — last ${relativeTime(entry.last_recalled)} ago` : ""}`}
          >
            ↻ {recall}
            {entry.last_recalled && (
              <span className="opacity-60 font-normal"> · {relativeTime(entry.last_recalled)}</span>
            )}
          </span>
        ) : (
          <span className="opacity-40" title="never recalled in a session">↻ 0</span>
        )}
        {Math.abs(effectiveness) >= 0.1 && (
          <span
            className={effectiveness > 0
              ? "text-[color:var(--accent-emerald-fg)]"
              : "text-[color:var(--accent-rose-fg)]"}
            title={`effectiveness ${effectiveness > 0 ? "+" : ""}${effectiveness.toFixed(2)} — outcome-correlated`}
          >
            {effectiveness > 0 ? "+" : ""}{effectiveness.toFixed(1)}
          </span>
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
      className="text-2xs uppercase tracking-wider font-mono px-1.5 py-0.5 rounded ring-1"
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
